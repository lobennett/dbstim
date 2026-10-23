import contextlib
import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dbstim import ApiError, Client, MetricFilter, Segment
from dbstim.workflow import _manifest
from tests.http_fixture import server
from tests.test_dstim import Fixture, manifest, response


class ValidationTests(unittest.TestCase):
    def test_segment_rejects_boolean_nonfinite_and_reversed_bounds(self):
        for bounds in ((True, 2), (0, float('inf')), (float('nan'), 2), (2, 1), (-1, 0)):
            with self.subTest(bounds=bounds), self.assertRaises(ValueError):
                Segment(*bounds)

    def test_filters_are_finite_and_serializable(self):
        self.assertEqual(MetricFilter('has_audio', 'is', True).to_dict()['value'], True)
        for values in (('', 'gt', 1), ('duration_seconds', '?', 1), ('duration_seconds', 'gt', float('nan'))):
            with self.subTest(values=values), self.assertRaises(ValueError):
                MetricFilter(*values)

    def test_bad_manifest_identity_hash_duplicate_and_bounds(self):
        original = manifest()
        _manifest(original, 'experiment-1', 'subject-001')
        for mutation in ('hash', 'duplicate', 'segment'):
            document = copy.deepcopy(original)
            if mutation == 'hash':
                document['blocks'][0]['trials'][0]['media_id'] = 'different'
            else:
                if mutation == 'duplicate':
                    document['blocks'][0]['trials'][1]['trial_id'] = 'trial-1'
                else:
                    document['blocks'][0]['trials'][1]['segment']['end_seconds'] = -1
                payload = {key: value for key, value in document.items() if key != 'manifest_sha256'}
                document['manifest_sha256'] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
            with self.subTest(mutation=mutation), self.assertRaises(ApiError):
                _manifest(document, 'experiment-1', 'subject-001')

    def test_invalid_counts_fail_before_query(self):
        fixture = Fixture()
        with server(fixture) as (url, requests):
            client = Client('demo', 'secret', url=url)
            for values in (dict(subjects=True), dict(shared_fraction=-1), dict(shared_fraction=True),
                           dict(foils_per_run=10), dict(repeats_per_run=10), dict(seed=True), dict(seed=1.5),
                           dict(sessions_per_subject=0), dict(trials_per_run=0)):
                with self.subTest(values=values), self.assertRaises(ValueError):
                    client.create_experiment('Demo', **(dict(subjects=1, trials_per_run=5) | values))
            self.assertEqual(len(requests), 1)

    def test_manifest_rejects_malformed_or_duplicate_runs(self):
        for mutation in ('not-object', 'missing-trials', 'duplicate-run'):
            document = manifest()
            if mutation == 'not-object':
                document['blocks'][0] = None
            elif mutation == 'missing-trials':
                del document['blocks'][0]['trials']
            else:
                document['blocks'][0]['run_id'] = 'run-001'
                document['blocks'].append(dict(block_index=2, run_id='run-001', trials=[]))
            payload = {key: value for key, value in document.items() if key != 'manifest_sha256'}
            document['manifest_sha256'] = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
            with self.subTest(mutation=mutation), self.assertRaises(ApiError):
                _manifest(document, 'experiment-1', 'subject-001')

    def test_ambiguous_name_and_exact_revision(self):
        fixture = Fixture()

        def respond(method, path, headers, body):
            if path.startswith('/api/v1/experiments?'):
                return response(dict(experiments=[dict(id='one', name='Demo'), dict(id='two', name='Demo')]))
            return fixture(method, path, headers, body)

        with server(respond) as (url, _):
            client = Client('demo', 'secret', url=url)
            with self.assertRaisesRegex(ValueError, "2 experiments are named 'Demo': one \\(draft, created at an "
                                                    "unknown time\\); two .*client.experiment\\(id=...\\)"):
                client.experiment('Demo')
            with self.assertRaisesRegex(ValueError, "No experiment is named 'Other'; client.experiments"):
                client.experiment('Other')
            self.assertEqual(client.experiment(id='experiment-1').id, 'experiment-1')

    def test_download_reuses_verified_cache_and_never_follows_symlinks(self):
        with server(Fixture()) as (url, requests), tempfile.TemporaryDirectory() as temporary:
            client = Client('demo', 'secret', url=url)
            session = client.experiment('Demo').subject('subject-001').session()
            destination = Path(temporary) / 'nested' / 'download'
            session.download(destination)
            before = len(requests)
            session.download(destination)
            self.assertEqual(len(requests), before)
            alias = Path(temporary) / 'alias'
            alias.symlink_to(destination, target_is_directory=True)
            with self.assertRaises(ValueError):
                session.download(alias)
            trial_path = destination / 'trial-1.mp4'
            trial_path.unlink()
            trial_path.symlink_to(destination / 'trial-2.mp4')
            with self.assertRaises(ApiError):
                session.run('run-001').media(session.run('run-001').trials[0])

    def test_bad_checksum_leaves_unrelated_files_and_no_partial_video(self):
        fixture = Fixture()

        def respond(method, path, headers, body):
            if path.endswith('/media'):
                return 200, {'Content-Type': 'video/mp4', 'Content-Length': 3, 'X-Content-SHA256': 'a' * 64}, b'bad'
            return fixture(method, path, headers, body)

        with server(respond) as (url, _), tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'keep.txt').write_text('keep')
            session = Client('demo', 'secret', url=url).experiment('Demo').subject('subject-001').session()
            with self.assertRaisesRegex(ApiError, 'checksum'):
                session.download(root)
            self.assertEqual([path.name for path in root.iterdir()], ['keep.txt'])

    def test_separate_program_reuses_directory_and_recovers_orphan_receipt(self):
        with server(Fixture()) as (url, _), tempfile.TemporaryDirectory() as temporary:
            subject = Client('demo', 'secret', url=url).experiment('Demo').subject('subject-001')
            subject.session().download(temporary)
            (Path(temporary) / 'trial-1.json').unlink()
            reopened = subject.session(directory=temporary)
            reopened.download(temporary)
            run = reopened.run('run-001')
            self.assertEqual(run.media(run.trials[0]).read_bytes(), b'first-half')

    def test_download_does_not_reopen_a_named_temporary_file(self):
        with server(Fixture()) as (url, _), tempfile.TemporaryDirectory() as temporary:
            session = Client('demo', 'secret', url=url).experiment('Demo').subject('subject-001').session()
            with patch('dbstim.workflow.tempfile.NamedTemporaryFile', side_effect=PermissionError('Windows sharing violation')):
                session.download(temporary)

    def test_default_cache_is_per_user_and_shared_by_programs_in_any_directory(self):
        with server(Fixture()) as (url, requests), tempfile.TemporaryDirectory() as temporary:
            cache, first, second = (Path(temporary) / name for name in ('cache', 'notebook', 'task'))
            first.mkdir()
            second.mkdir()
            with patch.dict(os.environ, XDG_CACHE_HOME=str(cache)):
                with contextlib.chdir(first):
                    folder = Client('demo', 'secret', url=url).experiment('Demo').subjects[0].sessions[0].download()
                self.assertTrue(folder.is_relative_to(cache.resolve() / 'dbstim'))
                downloads = sum(path.endswith('/media') for _, path, _, _ in requests)
                with contextlib.chdir(second):
                    session = Client('demo', 'secret', url=url).experiment('Demo').subjects[0].sessions[0]
                    run = session.next_run()
                    self.assertEqual([run.media(trial, mode='local').parent for trial in run.trials], [folder] * 3)
                self.assertEqual(sum(path.endswith('/media') for _, path, _, _ in requests), downloads)
                self.assertEqual(list(first.iterdir()) + list(second.iterdir()), [])
                self.assertEqual(oct((cache / 'dbstim').stat().st_mode & 0o777), oct(0o700))
                run.media(run.trials[0]).write_bytes(b'tampered')
                with self.assertRaisesRegex(ApiError, 'does not match its verified receipt'):
                    run.media(run.trials[0], mode='local')
            with server(Fixture()) as (other, _), patch.dict(os.environ, XDG_CACHE_HOME=str(cache)):
                elsewhere = Client('demo', 'secret', url=other).experiment('Demo').subjects[0].sessions[0].download()
                self.assertNotEqual(elsewhere, folder)
            with patch.dict(os.environ, XDG_CACHE_HOME='relative/cache'), patch('pathlib.Path.home', return_value=cache):
                session = Client('demo', 'secret', url=url).experiment('Demo').subjects[0].sessions[0]
                self.assertTrue(session.download().is_relative_to(cache.resolve() / '.cache' / 'dbstim'))

    def test_local_disk_errors_are_disk_errors_and_leave_no_partial_files(self):
        failures = ((patch('dbstim.workflow.Path.write_text', side_effect=OSError(errno.ENOSPC, 'No space left on device')),
                     errno.ENOSPC, 'Could not save media in .*: No space left on device$'),
                    (patch('dbstim.workflow.os.link', side_effect=OSError(errno.ENOTSUP, 'Operation not supported')),
                     errno.ENOTSUP, 'cannot hard-link files \\(for example exFAT or FAT\\); choose a directory on your main disk$'))
        for failure, code, message in failures:
            with server(Fixture()) as (url, _), tempfile.TemporaryDirectory() as temporary:
                run = Client('demo', 'secret', url=url).experiment('Demo').subject('subject-001').session(
                    directory=temporary).next_run()
                with self.subTest(code=code), failure, self.assertRaisesRegex(OSError, message) as caught:
                    run.media(run.trials[0])
                self.assertEqual(caught.exception.errno, code)
                self.assertNotIsInstance(caught.exception, ApiError)
                self.assertEqual(list(Path(temporary).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
