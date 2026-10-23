"""Journeys over localhost HTTP against the real server code on temporary databases.

Opt in with DBSTIM_SERVER (see tests/real_server.py); otherwise every test is skipped.
"""

import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import dbstim
from dbstim import ApiError, Client, MetricFilter
from tests.real_server import ACCOUNTS, CHECKOUT, PYTHON, FaultProxy, count, real_server

SOURCE = str(Path(dbstim.__file__).resolve().parents[1])
NOTEBOOK = Path(__file__).resolve().parents[1] / 'notebooks' / 'api.ipynb'
# Runs the notebook in a fresh kernel; a first cell answers its prompts and points it at the fixture.
NOTEBOOK_RUNNER = r'''
import json, sys
import nbformat
from nbclient import NotebookClient

path, origin, name, password, directory = sys.argv[1:]
notebook = nbformat.read(path, as_version=4)
notebook.cells.insert(0, nbformat.v4.new_code_cell(f"""
import functools
import dbstim, dbstim.client
answers = {{'dbstimuli.com username: ': 'admin', 'Experiment name: ': {name!r}}}
input = dbstim.client.input = lambda prompt='': answers[prompt]  # The kernel cannot prompt.
dbstim.client.getpass = lambda prompt='': {password!r}
dbstim.connect = functools.partial(dbstim.connect, url={origin!r})
"""))
NotebookClient(notebook, kernel_name='python3', timeout=120, resources=dict(metadata=dict(path=directory))).execute()
print(json.dumps(['\n'.join(output.get('text', '') or output.get('data', {}).get('text/plain', '')
                            for output in cell.get('outputs', [])) for cell in notebook.cells[1:]]))
'''


def present_all(session, presented):
    """The README loop, with a stand-in for the task's presentation function."""
    while (run := session.next_run()) is not None:
        while (trial := run.next_trial()) is not None:
            run.media(trial)
            run.record(trial, 'started')
            presented.append(trial.trial_id)
            run.record(trial, 'completed', result={'rt': trial.position})


@unittest.skipUnless(CHECKOUT, 'Set DBSTIM_SERVER to a dbp-dataset-browser checkout to run real-server tests')
class RealServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.origin, cls.root = cls.enterClassContext(real_server())

    def setUp(self):
        self.proxy = FaultProxy(self.origin)
        self.addCleanup(self.proxy.close)
        cache = tempfile.TemporaryDirectory()
        self.addCleanup(cache.cleanup)
        self.cache = Path(cache.name)
        self.enterContext(patch.dict(os.environ, XDG_CACHE_HOME=cache.name))
        self.enterContext(patch('dbstim.workflow._RETRY_DELAYS', (0.01, 0.01, 0.01)))
        self.client = Client('admin', ACCOUNTS['admin'], url=self.proxy.origin)

    def publish(self, name, **settings):
        settings = dict(subjects=1, runs_per_session=2, trials_per_run=2, seed=name) | settings
        return self.client.create_experiment(name, **settings).publish()

    def reopen(self, experiment):
        """The same session as a separate program sees it."""
        return Client('admin', ACCOUNTS['admin'], url=self.proxy.origin).experiment(id=experiment.id).subjects[0].sessions[0]

    def status(self, session):
        return session._http._json('GET', session._path)

    def posts(self, suffix):
        return [json.loads(body) for method, path, _, body in self.proxy.seen if method == 'POST' and path.endswith(suffix)]

    def media_requests(self):
        return sum(path.endswith('/media') for _, path, _, _ in self.proxy.seen)

    def test_readme_loop_completes_and_session_results_stay_readable(self):
        experiment = self.publish('Readme')
        session = experiment.subjects[0].sessions[0]
        presented = []
        with session:
            present_all(session, presented)
        expected = [trial.trial_id for run_id in session.run_ids for trial in session.run(run_id).trials]
        self.assertEqual(presented, expected)
        self.assertEqual(self.status(session)['status'], 'completed')
        self.assertIsNone(session.next_run())
        self.assertEqual([(row['trial_id'], row['result']) for row in session.results],
                         [(trial.trial_id, {'rt': trial.position}) for run_id in session.run_ids
                          for trial in session.run(run_id).trials])
        with self.assertRaisesRegex(ApiError, 'HTTP 409: Session is completed'):
            session.start()

    def test_interrupted_trial_is_abandoned_and_the_run_continues_without_replay(self):
        experiment = self.publish('Abandon')
        session = experiment.subjects[0].sessions[0]
        if 'abandoned' not in self.status(session):
            self.skipTest('This server checkout predates abandoned trials')
        presented = []
        with self.assertRaises(KeyboardInterrupt), session:
            while (run := session.next_run()) is not None:
                while (trial := run.next_trial()) is not None:
                    run.record(trial, 'started')
                    presented.append(trial.trial_id)
                    if len(presented) == 2:
                        raise KeyboardInterrupt  # Ctrl-C while the second video plays
                    run.record(trial, 'completed', result={'seen': True})
        self.assertEqual(self.status(session)['status'], 'interrupted')
        resumed = self.reopen(experiment)
        with resumed:
            [interrupted] = resumed.unconfirmed
            self.assertEqual(interrupted.trial_id, presented[1])
            with self.assertRaisesRegex(ApiError, rf'{interrupted.trial_id} \(run-001, position 2\).*"abandoned"'):
                resumed.next_run().next_trial()
            resumed.record(interrupted, 'abandoned', result={'reason': 'KeyboardInterrupt'})
            self.assertEqual(resumed.unconfirmed, ())
            run = resumed.next_run()
            skipped = run.next_trial()  # Abandoned without a result.
            run.record(skipped, 'started')
            presented.append(skipped.trial_id)
            run.record(skipped, 'abandoned')
            present_all(resumed, presented)
        self.assertEqual(len(presented), 4)
        self.assertEqual(presented.count(interrupted.trial_id), 1)
        state = self.status(resumed)
        abandoned = sorted([interrupted.trial_id, skipped.trial_id])
        self.assertEqual((state['status'], sorted(state['abandoned']), state['unconfirmed']), ('interrupted', abandoned, []))
        for view in (resumed, self.reopen(experiment)):
            self.assertEqual(sorted(trial.trial_id for trial in view.abandoned), abandoned)
            self.assertEqual([(row['trial_id'], row['status']) for row in view.results if row['status'] == 'abandoned'],
                             [(interrupted.trial_id, 'abandoned')])
        with resumed:  # Nothing is left to present, and an abandoned trial still means interrupted.
            self.assertIsNone(resumed.next_run())
            with self.assertRaisesRegex(ApiError, 'HTTP 409'):
                resumed.record(interrupted, 'started')
            with self.assertRaisesRegex(ApiError, 'HTTP 409: A session with abandoned trials cannot be completed'):
                resumed.finish('completed')
        self.assertEqual(self.status(resumed)['status'], 'interrupted')
        summary = self.client._http._json('GET', experiment._path + '/progress')['subjects'][0]
        self.assertEqual((summary['completed'], summary['abandoned'], summary['unconfirmed']), (2, 2, 0))

    def test_server_without_abandoned_outcomes_refuses_clearly_and_changes_nothing(self):
        with real_server(legacy_events=True) as (origin, root):
            client = Client('admin', ACCOUNTS['admin'], url=origin)
            session = client.create_experiment('Legacy', subjects=1, trials_per_run=2).publish().subjects[0].sessions[0]
            with session:
                run = session.next_run()
                trial = run.next_trial()
                run.record(trial, 'started')
                events = count(root, 'experiment_trial_events')
                with self.assertRaisesRegex(ApiError, 'upgrade') as caught:
                    session.record(trial, 'abandoned')
                self.assertIn(caught.exception.status, (400, 409))
                self.assertEqual(count(root, 'experiment_trial_events'), events)
                self.assertEqual((session.unconfirmed, session.abandoned), ((trial,), ()))
                run.record(trial, 'completed', result={'seen': True})  # It was shown, so record it honestly.
                self.assertEqual(run.next_trial(), run.trials[1])

    def test_lost_request_or_reply_is_resent_with_the_same_request_id_and_recorded_once(self):
        for fault in ('request', 'response', '503', 'response:502'):
            with self.subTest(fault=fault):
                experiment = self.publish('Retry ' + fault)
                session = experiment.subjects[0].sessions[0]
                self.proxy.faults.append(('POST', '/start', fault))
                with session:
                    run = session.next_run()
                    trial = run.next_trial()
                    run.record(trial, 'started')
                    events = count(self.root, 'experiment_trial_events')
                    commands = count(self.root, 'experiment_session_commands')
                    before = len(self.posts('/trials'))
                    self.proxy.faults.append(('POST', '/trials', fault))
                    run.record(trial, 'completed', result={'seen': True})
                    attempts = self.posts('/trials')[before:]
                    self.assertEqual(len(attempts), 2)
                    self.assertEqual(attempts[0], attempts[1])
                    self.assertEqual(count(self.root, 'experiment_trial_events'), events + 1)
                    self.assertEqual(count(self.root, 'experiment_session_commands'), commands + 1)
                    self.assertEqual([row['trial_id'] for row in session.results], [trial.trial_id])
                    self.assertEqual(run.next_trial(), run.trials[1])
                [first, second] = self.posts('/start')[-2:]
                self.assertEqual(first, second)
                self.assertEqual(self.status(session)['status'], 'interrupted')

    def test_unacknowledged_command_stays_pending_until_repeated(self):
        experiment = self.publish('Pending')
        session = experiment.subjects[0].sessions[0]
        session.start()
        run = session.next_run()
        trial = run.next_trial()
        run.record(trial, 'started')
        self.proxy.faults.extend([('POST', '/trials', 'request')] * 4)
        with self.assertRaisesRegex(ApiError, rf"record\({trial.trial_id}, 'completed'\) was not acknowledged after 4 "
                                              'attempts. Repeat that call'):
            run.record(trial, 'completed', result={'seen': True})
        for refused in (session.next_run, lambda: run.record(run.trials[1], 'started'), session.finish):
            with self.assertRaisesRegex(ApiError, 'not acknowledged'):
                refused()
        run.record(trial, 'completed', result={'seen': True})
        self.assertEqual(len({attempt['request_id'] for attempt in self.posts('/trials')[-5:]}), 1)
        self.assertEqual([(row['trial_id'], row['result']) for row in session.results], [(trial.trial_id, {'seen': True})])

    def test_crashed_program_is_replaced_only_by_explicit_takeover(self):
        experiment = self.publish('Takeover')
        session = experiment.subjects[0].sessions[0]
        with self.assertRaisesRegex(ApiError, 'not acknowledged after 4 attempts') as caught:
            with session:
                run = session.next_run()
                trial = run.next_trial()
                run.record(trial, 'started')
                self.proxy.faults.extend([('POST', '/trials', 'request')] * 4)
                run.record(trial, 'completed', result={'seen': True})
        self.assertIn('could not be marked failed', caught.exception.__notes__[0])
        rerun = self.reopen(experiment)  # The first program has exited without releasing its claim.
        with self.assertRaisesRegex(ApiError, r'Another presenter.*session\.start\(takeover=True\)'):
            rerun.start()
        rerun.start(takeover=True)
        presented = []
        with rerun:
            [shown] = rerun.unconfirmed
            rerun.record(shown, 'completed', result={'seen': True})  # It was shown to the end.
            present_all(rerun, presented)
        self.assertNotIn(shown.trial_id, presented)
        self.assertEqual(self.status(rerun)['status'], 'completed')

    def test_resent_start_after_another_takeover_is_refused_by_the_claim_check(self):
        experiment = self.publish('Claim')
        session = experiment.subjects[0].sessions[0]
        session.start()
        run = session.next_run()
        self.proxy.faults.extend([('POST', '/trials', 'response')] * 4)
        with self.assertRaisesRegex(ApiError, 'not acknowledged after 4 attempts'):
            run.record(run.trials[0], 'started')
        events = count(self.root, 'experiment_trial_events')
        self.reopen(experiment).start(takeover=True)
        with self.assertRaisesRegex(ApiError, 'HTTP 409: This presentation claim is no longer active'):
            run.record(run.trials[0], 'started')
        self.assertEqual(count(self.root, 'experiment_trial_events'), events)
        self.assertEqual(self.status(session)['unconfirmed'], [run.trials[0].trial_id])

    def test_client_errors_and_unserializable_results_are_never_sent_twice(self):
        session = self.publish('No resend').subjects[0].sessions[0]
        with session:
            run = session.next_run()
            trial = run.next_trial()
            before = len(self.posts('/trials'))
            with self.assertRaises(ApiError) as caught:
                run.record(trial, 'completed')  # Never started.
            self.assertEqual((caught.exception.status, len(self.posts('/trials'))), (409, before + 1))
            run.record(trial, 'started')
            for result in ({'rt': float('nan')}, {'count': object()}):
                with self.assertRaisesRegex(ValueError, 'plain JSON'):
                    run.record(trial, 'completed', result=result)
            self.assertEqual(len(self.posts('/trials')), before + 2)
            self.assertEqual(session.unconfirmed, (trial,))
            run.record(trial, 'completed', result={'rt': None})

    def test_verified_cache_is_shared_by_programs_in_any_directory(self):
        experiment = self.publish('Cache')
        program = ('import sys; from dbstim import Client; '
                   'session = Client("admin", sys.argv[1], url=sys.argv[2]).experiment(id=sys.argv[3]).subjects[0].sessions[0]; '
                   'run = session.next_run(); '
                   'print(session.download() if sys.argv[4] == "download" else run.media(run.trials[0], mode="local"))')

        def run_program(directory, action):
            return subprocess.run([sys.executable, '-c', program, ACCOUNTS['admin'], self.proxy.origin, experiment.id,
                                   action], cwd=directory, env=dict(os.environ, PYTHONPATH=SOURCE),
                                  capture_output=True, text=True, timeout=60)

        with tempfile.TemporaryDirectory() as notebook, tempfile.TemporaryDirectory() as task:
            downloaded = run_program(notebook, 'download')
            self.assertEqual(downloaded.returncode, 0, downloaded.stderr)
            self.assertEqual(self.media_requests(), 4)
            presented = run_program(task, 'local')
            self.assertEqual(presented.returncode, 0, presented.stderr)
            self.assertEqual(self.media_requests(), 4)
            path = Path(presented.stdout.strip())
            self.assertEqual(path.parent, Path(downloaded.stdout.strip()))
            self.assertTrue(path.is_relative_to(self.cache.resolve() / 'dbstim'))
            self.assertEqual(list(Path(notebook).iterdir()) + list(Path(task).iterdir()), [])
            path.write_bytes(b'tampered')
            refused = run_program(task, 'local')
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn('does not match its verified receipt', refused.stderr)
            self.assertEqual(path.read_bytes(), b'tampered')
            self.assertEqual(self.media_requests(), 4)

    @unittest.skipIf(hasattr(os, 'geteuid') and os.geteuid() == 0, 'Permissions do not restrict root')
    def test_local_disk_errors_are_reported_as_disk_errors(self):
        subject = self.publish('Disk').subjects[0]
        folder = Path(self.enterContext(tempfile.TemporaryDirectory())) / 'read-only'
        run = subject.session('session-001', directory=folder).next_run()
        folder.chmod(stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(folder.chmod, stat.S_IRWXU)
        with self.assertRaisesRegex(PermissionError, 'Could not save media in .*read-only: Permission denied'):
            run.media(run.trials[0])
        folder.chmod(stat.S_IRWXU)
        with patch('dbstim.workflow.os.link', side_effect=OSError(errno.ENOTSUP, 'Operation not supported')), \
                self.assertRaisesRegex(OSError, 'cannot hard-link files'):
            run.media(run.trials[0])
        self.assertEqual(list(folder.iterdir()), [])
        self.assertEqual(run.media(run.trials[0]).parent, folder.resolve())

    def test_errors_and_displays_are_actionable(self):
        with self.assertRaisesRegex(ApiError, '^Sign-in failed: check your username and password$'):
            Client('admin', 'wrong-password', url=self.proxy.origin)
        experiment = self.publish('Display')
        with self.assertRaisesRegex(ApiError, 'HTTP 404: Subject not found'):
            experiment.subject('subject-999')
        self.proxy.faults.append(('GET', '/api/v1/metrics', '503'))
        with self.assertRaisesRegex(ApiError, 'HTTP 503: Injected server fault; retry shortly'):
            self.client.metrics()
        twice = [self.client.create_experiment('Twice', subjects=1, trials_per_run=2).id for _ in range(2)]
        with self.assertRaisesRegex(ValueError, "2 experiments are named 'Twice'.*client.experiment\\(id=") as caught:
            self.client.experiment('Twice')
        self.assertTrue(all(experiment_id in str(caught.exception) for experiment_id in twice))
        metrics = {metric.id: repr(metric) for metric in self.client.metrics()}
        self.assertTrue(metrics['audio_track_present'].endswith('(boolean); operators == !=>'))
        self.assertTrue(metrics['duration_seconds'].endswith('seconds); operators > < >= <=>'))
        query = dict(filters=[MetricFilter('audio_track_present', '==', True).to_dict(),
                              MetricFilter('audio_track_present', '!=', False).to_dict()],
                     content_query='', corpus='both', mode='keyword', custom_metrics=[], limit=1)
        self.assertIn('matching', self.client._http._json('POST', '/api/v1/media/query', query))
        subject = experiment.subjects[0]
        session = subject.sessions[0]
        before = len(self.proxy.seen)
        displays = [repr(item) for item in (self.client, experiment, subject, session, session.run('run-001'))]
        self.assertEqual(len(self.proxy.seen), before)
        self.assertEqual(displays, [f'<Client admin at {self.proxy.origin}>', f"<Experiment 'Display' {experiment.id}>",
                                    '<Subject subject-001: 1 session>',
                                    '<Session session-001 (subject-001): run-001, run-002>',
                                    '<Run run-001 (session-001): 2 trials>'])
        with self.assertWarnsRegex(UserWarning, r'shared_fraction=0.1 rounds down to 0 of 4 .*realized fraction 0\)'):
            self.client.create_experiment('Shared', subjects=1, trials_per_run=4, shared_fraction=0.1)

    def test_notebook_runs_in_an_isolated_kernel_before_and_after_every_trial_is_completed(self):
        if subprocess.run([PYTHON, '-c', 'import nbclient, ipykernel'], capture_output=True).returncode:
            self.skipTest('DBSTIM_SERVER_PYTHON lacks nbclient and ipykernel')
        experiment = self.publish('Notebook walkthrough')

        def run_notebook():
            with tempfile.TemporaryDirectory() as directory:
                environment = dict(os.environ, PYTHONPATH=SOURCE, IPYTHONDIR=directory + '/.ipython',
                                   JUPYTER_RUNTIME_DIR=directory + '/.runtime', JUPYTER_PLATFORM_DIRS='1')
                finished = subprocess.run([PYTHON, '-c', NOTEBOOK_RUNNER, str(NOTEBOOK), self.proxy.origin,
                                           'Notebook walkthrough', ACCOUNTS['admin'], directory],
                                          cwd=directory, env=environment, capture_output=True, text=True, timeout=300)
                self.assertEqual(finished.returncode, 0, finished.stderr[-3000:])
                self.assertLessEqual({path.name for path in Path(directory).iterdir()}, {'.ipython', '.runtime'})
                return json.loads(finished.stdout)

        before = run_notebook()
        self.assertIn("<Experiment 'Notebook walkthrough'", before[10])
        self.assertIn("Trial(trial_id='trial-000001'", before[12])
        self.assertIn('Video', before[14])
        self.assertEqual(before[18], '[]')
        media = self.media_requests()
        presented = []
        with self.reopen(experiment) as session:
            present_all(session, presented)
        self.assertEqual(self.media_requests(), media)  # The notebook's download is reused.
        after = run_notebook()
        self.assertEqual(after[12], "'Every trial is completed'")
        self.assertEqual(after[14], '')
        self.assertIn("'result': {'rt': 1}", after[18])
        self.assertEqual(after[18].count("'status': 'completed'"), 4)


if __name__ == '__main__':
    unittest.main()
