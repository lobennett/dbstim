import hashlib
import json
import tempfile
import unittest

from dbstim import ApiError, Client, Experiment, MetricFilter, Run, Session, Subject, Trial
from tests.http_fixture import server


def response(document, status=200, **headers):
    return status, {'Content-Type': 'application/json', **headers}, json.dumps(document).encode()


def manifest(blocks=None):
    trials = [dict(trial_id=f'trial-{index}', media_id='same-video', media_type='video',
                   role=role, segment=dict(start_seconds=start, end_seconds=end))
              for index, (role, start, end) in enumerate([('parent', 0, 5), ('repeat', 0, 5), ('foil', 5, 10)], 1)]
    document = dict(schema_version='dbp-experiment-v1', experiment_id='experiment-1', subject_id='subject-001',
                    blocks=blocks or [dict(block_index=1, trials=trials)])
    digest = hashlib.sha256(json.dumps(document, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
    return dict(document, manifest_sha256=digest)


class Fixture:
    def __init__(self, document=None):
        self.document = document or manifest()
        self.states = {}
        self.commands = {}
        self.lost_reply = False

    def __call__(self, method, path, headers, body):
        document = json.loads(body) if body else {}
        if path == '/api/auth/login':
            return response(dict(csrf_token='csrf', user=dict(username='demo')), **{'Set-Cookie': 'dbp_session=secret; Path=/; HttpOnly'})
        if path == '/api/v1/metrics':
            return response(dict(metrics=[dict(id='duration_seconds')]))
        if path.startswith('/api/v1/experiments?'):
            return response(dict(experiments=[dict(id='experiment-1', name='Demo', latest_published_revision_id='experiment-1')]))
        if path == '/api/v1/media/query':
            return response(dict(version='catalog-version'))
        if path == '/api/v1/experiments' and method == 'POST':
            self.created = document
            return response(dict(id='experiment-1'), 201)
        if path.endswith('/publish'):
            return response(dict(subjects=[self.document]))
        if path == '/api/v1/experiments/experiment-1':
            return response(dict(id='experiment-1', settings=dict(name='Demo', subject_count=1),
                                 publication=dict(subjects=[self.document])))
        if path.endswith('/manifest'):
            return response(self.document)
        if path.endswith('/media'):
            payload = b'second-half' if '/trial-3/' in path else b'first-half'
            return 200, {'Content-Type': 'video/mp4', 'Content-Length': len(payload),
                         'X-Content-SHA256': hashlib.sha256(payload).hexdigest()}, payload
        if path.endswith('/media-access'):
            return response(dict(path='/api/v1/media-access/' + 'a' * 64, expires_at=9999999999, sha256='b' * 64, size_bytes=99))
        if '/sessions/session-001' in path:
            if method == 'GET':
                return response(dict(manifest_sha256=self.document['manifest_sha256'], session_id='session-001',
                                     runs=[dict(run_id=f"run-{block['block_index']:03d}", trials=block['trials'])
                                           for block in self.document['blocks']],
                                     latest_run_id='run-001', progress=self.states.copy(),
                                     unconfirmed=[key for key, value in self.states.items() if value['status'] == 'started']))
            self.commands[document['request_id']] = document
            if path.endswith('/trials'):
                self.states[document['trial_id']] = dict(status=document['status'])
            if self.lost_reply:
                self.lost_reply = False
                return response({}, 503)
            return response(dict(accepted=document['request_id']))
        raise AssertionError(path)


class WorkflowTests(unittest.TestCase):
    def test_typed_objects_server_resume_and_media(self):
        fixture = Fixture()
        with server(fixture) as (url, requests), tempfile.TemporaryDirectory() as directory:
            with Client('demo', 'secret', url=url) as client:
                self.assertEqual(client.metrics()[0].id, 'duration_seconds')
                experiment = client.experiment('Demo')
                self.assertIsInstance(experiment, Experiment)
                subject = experiment.subject('subject-001')
                self.assertIsInstance(subject, Subject)
                session = subject.session('session-001')
                self.assertIsInstance(session, Session)
                run = session.run()
                self.assertIsInstance(run, Run)
                trial = run.next_trial()
                self.assertIsInstance(trial, Trial)
                self.assertEqual(trial.segment.end_seconds, 5)
                session.download(directory)
                for item in run.trials:
                    self.assertTrue(run.media(item).is_file())
                self.assertTrue(run.media(trial, mode='remote').startswith(url + '/api/v1/media-access/'))
                session.start()
                run.record(trial, 'started')
                with self.assertRaisesRegex(ApiError, 'unconfirmed'):
                    session.run().next_trial()
                run.record(trial, 'completed', result={'answer': True})
                other = Client('demo', 'secret', url=url)
                self.assertEqual(other.experiment('Demo').subject('subject-001').session().run().next_trial().trial_id, 'trial-2')
                session.finish('interrupted')
                hashes = {hashlib.sha256(run.media(item).read_bytes()).hexdigest() for item in run.trials}
                self.assertEqual(len(hashes), 2)
                self.assertEqual(run.media(run.trials[0]).read_bytes(), run.media(run.trials[1]).read_bytes())
                self.assertEqual(len({run.media(item) for item in run.trials}), 3)
                run.media(trial).write_bytes(b'corrupted')
                with self.assertRaises(ApiError):
                    run.media(trial)
                self.assertTrue(any(item[2].get('Cookie') == 'dbp_session=secret' for item in requests))

    def test_lost_ack_retries_same_command_and_blocks_advancement(self):
        fixture = Fixture()
        with server(fixture) as (url, _):
            client = Client('demo', 'secret', url=url)
            session = client.experiment('Demo').subject('subject-001').session()
            session.start()
            run = session.run()
            trial = run.next_trial()
            fixture.lost_reply = True
            with self.assertRaises(ApiError):
                run.record(trial, 'started')
            before = len(fixture.commands)
            with self.assertRaises(ApiError):
                run.next_trial()
            run.record(trial, 'started')
            self.assertEqual(len(fixture.commands), before)
            run.record(trial, 'completed')
            self.assertEqual(run.next_trial().trial_id, 'trial-2')

    def test_create_is_draft_and_pins_query(self):
        fixture = Fixture()
        with server(fixture) as (url, requests):
            client = Client('demo', 'secret', url=url)
            experiment = client.create_experiment('Demo', subjects=5, trials_per_run=24, foils_per_run=4,
                sessions_per_subject=2, runs_per_session=3, shared_fraction=0.1, seed='fixed',
                filters=[MetricFilter('duration_seconds', 'gte', 10)])
            self.assertIsInstance(experiment, Experiment)
            self.assertEqual(fixture.created['version'], 'catalog-version')
            self.assertEqual(fixture.created['settings']['shared_fraction'], 0.1)
            self.assertFalse(any(path.endswith('/publish') for _, path, _, _ in requests))


if __name__ == '__main__':
    unittest.main()
