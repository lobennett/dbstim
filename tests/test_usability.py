from contextlib import nullcontext
from decimal import Decimal
import tempfile
import unittest
from unittest.mock import patch

import dbstim
from dbstim import ApiError, Client, Metric, MetricFilter, Run, Session, Subject
from tests.http_fixture import server
from tests.test_dstim import Fixture, manifest, response


def two_runs():
    def block(index):
        return dict(block_index=index, trials=[dict(trial_id=f'run{index}-trial-{position}', media_id='video',
                                                    media_type='video', role='parent', segment=None)
                                               for position in (1, 2)])
    return manifest([block(1), block(2)])


def finishes(fixture):
    return [command['status'] for command in fixture.commands.values() if 'trial_id' not in command and 'status' in command]


class UsabilityTests(unittest.TestCase):
    def test_symbolic_operators_map_to_server_codes(self):
        for symbol, code in (('>=', 'gte'), ('<=', 'lte'), ('>', 'gt'), ('<', 'lt'), ('==', 'eq'), ('gte', 'gte')):
            with self.subTest(symbol=symbol):
                self.assertEqual(MetricFilter('duration_seconds', symbol, 10).to_dict()['operator'], code)
        self.assertEqual(MetricFilter('duration_seconds', '>=', 10), MetricFilter('duration_seconds', 'gte', 10))
        for symbol, value, code in (('==', True, 'is'), ('!=', True, 'is_not'), ('==', False, 'is'),
                                    ('is', True, 'is'), ('==', None, 'eq'), ('==', 1, 'eq')):
            with self.subTest(symbol=symbol, value=value):
                self.assertEqual(MetricFilter('audio_track_present', symbol, value).operator, code)
        for symbol, value in (('=>', 10), ('!=', 10), ('!=', None), (['=='], True)):
            with self.subTest(symbol=symbol, value=value), self.assertRaises(ValueError):
                MetricFilter('duration_seconds', symbol, value)

    def test_integer_seed_is_sent_as_text(self):
        fixture = Fixture()
        with server(fixture) as (url, _):
            Client('demo', 'secret', url=url).create_experiment('Demo', subjects=1, trials_per_run=3, seed=7)
            self.assertEqual(fixture.created['settings']['seed'], '7')

    def test_metrics_are_small_objects_with_readable_repr(self):
        def respond(method, path, headers, body):
            if path == '/api/v1/metrics':
                return response(dict(metrics=[dict(id='duration_seconds', label='Duration', kind='number',
                                                   unit='seconds', operators=['gt', 'gte'])], methods=[]))
            return Fixture()(method, path, headers, body)

        with server(respond) as (url, _):
            [metric] = Client('demo', 'secret', url=url).metrics()
        self.assertEqual(metric, Metric('duration_seconds', 'Duration', 'number', 'seconds', ('gt', 'gte')))
        self.assertEqual(repr(metric), '<Metric duration_seconds: Duration (number, seconds); operators > >=>')
        self.assertEqual(repr(Metric('audio_track_present', 'Audio track', 'boolean', None, ('is', 'is_not'))),
                         '<Metric audio_track_present: Audio track (boolean); operators == !=>')

    def test_connect_prompts_without_echoing_the_password(self):
        with server(Fixture()) as (url, requests):
            with patch('builtins.input', return_value=' demo ') as asked, \
                    patch('dbstim.client.getpass', return_value='secret') as hidden:
                client = dbstim.connect(url=url)
            self.assertEqual(client.username, 'demo')
            asked.assert_called_once()
            hidden.assert_called_once()
            with patch('builtins.input') as asked, patch('dbstim.client.getpass', return_value='secret'):
                dbstim.connect('demo', url=url)
            asked.assert_not_called()
            self.assertIn(b'"password": "secret"', requests[0][3])

    def test_server_lists_subjects_sessions_and_next_run(self):
        fixture = Fixture(two_runs())
        with server(fixture) as (url, requests):
            experiment = Client('demo', 'secret', url=url).experiment('Demo')
            self.assertEqual(experiment.summary()['subject_ids'], ['subject-001'])
            [subject] = experiment.subjects
            self.assertIsInstance(subject, Subject)
            self.assertFalse(any(path.endswith('/manifest') for _, path, _, _ in requests))
            self.assertIs(experiment.subjects, experiment.subjects)
            [session] = subject.sessions
            self.assertIsInstance(session, Session)
            self.assertIs(subject.sessions[0], session)
            self.assertEqual(experiment.subject('subject-001').session('session-001').run_ids, session.run_ids)
            run = session.next_run()
            self.assertIsInstance(run, Run)
            self.assertEqual(run.id, 'run-001')
            fixture.states.update({trial.trial_id: dict(status='completed') for trial in run.trials})
            self.assertEqual(session.next_run().id, 'run-002')
            self.assertEqual(session.run('latest').id, 'run-001')
            fixture.states.update({trial.trial_id: dict(status='completed') for trial in session.run('run-002').trials})
            self.assertIsNone(session.next_run())

    def test_subjects_require_a_publication(self):
        fixture = Fixture()

        def respond(method, path, headers, body):
            if path == '/api/v1/experiments/experiment-1':
                return response(dict(id='experiment-1', settings=dict(name='Demo', subject_count=2), publication=None))
            return fixture(method, path, headers, body)

        with server(respond) as (url, _):
            experiment = Client('demo', 'secret', url=url).experiment('Demo')
            with self.assertRaisesRegex(ApiError, 'Publish'):
                experiment.subjects
            self.assertEqual(experiment.summary()['subject_ids'], ['subject-001', 'subject-002'])

    def test_session_block_finishes_by_outcome(self):
        for outcome in ('completed', 'interrupted', 'failed'):
            fixture = Fixture()
            with self.subTest(outcome=outcome), server(fixture) as (url, _):
                session = Client('demo', 'secret', url=url).experiment('Demo').subjects[0].sessions[0]
                with self.assertRaises(KeyError) if outcome == 'failed' else nullcontext():
                    with session as entered:
                        self.assertIs(entered, session)
                        self.assertEqual([command for command in fixture.commands.values()
                                          if 'takeover' in command][0]['takeover'], False)
                        run = session.next_run()
                        trials = run.trials if outcome == 'completed' else run.trials[:1]
                        for trial in trials:
                            run.record(trial, 'started')
                            run.record(trial, 'completed', dict(rt=trial.position))
                        self.assertEqual(run.results, [dict(trial_id=trial.trial_id, status='completed',
                                                            result=dict(rt=trial.position)) for trial in trials])
                        if outcome == 'failed':
                            raise KeyError('presentation crashed')
                self.assertEqual(finishes(fixture), [outcome])

    def test_ctrl_c_finishes_interrupted_and_reraises(self):
        fixture = Fixture()
        with server(fixture) as (url, _):
            session = Client('demo', 'secret', url=url).experiment('Demo').subjects[0].sessions[0]
            with self.assertRaises(KeyboardInterrupt):
                with session:
                    while (run := session.next_run()) is not None:
                        for trial in run.trials:
                            run.record(trial, 'started')
                            run.record(trial, 'completed')
                    raise KeyboardInterrupt
            self.assertEqual(finishes(fixture), ['interrupted'])

    def test_failed_finish_keeps_the_original_error(self):
        fixture = Fixture()
        with server(fixture) as (url, _), patch('dbstim.workflow._RETRY_DELAYS', (0, 0, 0)):
            session = Client('demo', 'secret', url=url).experiment('Demo').subjects[0].sessions[0]
            with self.assertRaises(KeyError) as caught:
                with session:
                    run = session.next_run()
                    fixture.lost_reply = 4
                    with self.assertRaises(ApiError):
                        run.record(run.trials[0], 'started')
                    raise KeyError('stop')
            self.assertIn('could not be marked failed', caught.exception.__notes__[0])
            self.assertEqual(finishes(fixture), [])

    def test_media_downloads_and_verifies_by_default(self):
        with server(Fixture()) as (url, requests), tempfile.TemporaryDirectory() as directory:
            session = Client('demo', 'secret', url=url).experiment('Demo').subjects[0].session('session-001',
                                                                                              directory=directory)
            run = session.next_run()
            path = run.media(run.trials[0])
            self.assertEqual(path.read_bytes(), b'first-half')
            before = len(requests)
            self.assertEqual(run.media(run.trials[0]), path)
            self.assertEqual(len(requests), before)
            with self.assertRaisesRegex(ApiError, 'Download'):
                run.media(run.trials[2], mode='local')

    def test_interrupted_trial_is_listed_named_and_abandoned_without_replay(self):
        fixture = Fixture(two_runs())
        with server(fixture) as (url, _):
            session = Client('demo', 'secret', url=url).experiment('Demo').subjects[0].sessions[0]
            with self.assertRaises(KeyboardInterrupt), session:
                run = session.next_run()
                trial = run.next_trial()
                run.record(trial, 'started')
                raise KeyboardInterrupt
            resumed = Client('demo', 'secret', url=url).experiment('Demo').subjects[0].sessions[0]
            self.assertEqual(resumed.unconfirmed, (trial,))
            presented = []
            with resumed:
                with self.assertRaisesRegex(ApiError, r'run1-trial-1 \(run-001, position 1\) was started but neither '
                                                      r'completed nor abandoned.*session.record\(trial, "abandoned"\)'):
                    resumed.next_run().next_trial()
                resumed.record(trial, 'abandoned', result={'reason': 'KeyboardInterrupt'})
                self.assertEqual(resumed.unconfirmed, ())
                while (run := resumed.next_run()) is not None:
                    while (item := run.next_trial()) is not None:
                        run.record(item, 'started')
                        run.record(item, 'completed')
                        presented.append(item.trial_id)
            self.assertEqual(presented, ['run1-trial-2', 'run2-trial-1', 'run2-trial-2'])
            self.assertEqual(finishes(fixture), ['interrupted', 'interrupted'])
            self.assertEqual(resumed.results, [dict(trial_id='run1-trial-1', status='abandoned',
                                                    result={'reason': 'KeyboardInterrupt'})])
            with self.assertRaisesRegex(ValueError, 'started, completed or abandoned'):
                resumed.record(trial, 'skipped')

    def test_every_abandoned_trial_is_listed_with_or_without_a_result(self):
        fixture = Fixture(two_runs())
        with server(fixture) as (url, _):
            session = Client('demo', 'secret', url=url).experiment('Demo').subjects[0].sessions[0]
            presented = []
            with session:
                self.assertEqual(session.abandoned, ())
                while (run := session.next_run()) is not None:
                    while (trial := run.next_trial()) is not None:
                        run.record(trial, 'started')
                        presented.append(trial.trial_id)
                        if trial.trial_id == 'run1-trial-1':
                            run.record(trial, 'abandoned')
                        elif trial.trial_id == 'run2-trial-1':
                            run.record(trial, 'abandoned', result={'reason': 'looked away'})
                        else:
                            run.record(trial, 'completed')
            self.assertEqual(presented, ['run1-trial-1', 'run1-trial-2', 'run2-trial-1', 'run2-trial-2'])
            self.assertEqual(finishes(fixture), ['interrupted'])
            reopened = Client('demo', 'secret', url=url).experiment('Demo').subjects[0].sessions[0]
            for view in (session, reopened):
                self.assertEqual([trial.trial_id for trial in view.abandoned], ['run1-trial-1', 'run2-trial-1'])
                self.assertEqual(view.results, [dict(trial_id='run2-trial-1', status='abandoned',
                                                     result={'reason': 'looked away'})])
                self.assertIsNone(view.next_run())

    def test_server_without_abandoned_trials_is_named_and_leaves_the_trial_unconfirmed(self):
        fixture = Fixture()
        fixture.abandoned = False
        with server(fixture) as (url, _):
            session = Client('demo', 'secret', url=url).experiment('Demo').subjects[0].sessions[0]
            session.start()
            run = session.next_run()
            trial = run.next_trial()
            run.record(trial, 'started')
            with self.assertRaisesRegex(ApiError, 'HTTP 400: .* This server may predate abandoned trials; ask its '
                                                  'operator to upgrade it. trial-1 was not changed.'):
                run.record(trial, 'abandoned')
            self.assertEqual(session.unconfirmed, (trial,))

    def test_results_must_be_plain_json_and_bad_values_are_never_sent(self):
        fixture = Fixture()
        with server(fixture) as (url, _):
            session = Client('demo', 'secret', url=url).experiment('Demo').subjects[0].sessions[0]
            session.start()
            run = session.next_run()
            trial = run.next_trial()
            run.record(trial, 'started')
            sent = len(fixture.commands)
            for result in ({'rt': float('nan')}, {'rt': Decimal('0.5')}, {'answer': object()}, {(1, 2): 'key'}):
                with self.subTest(result=result), self.assertRaisesRegex(ValueError, 'plain JSON.*None for no response'):
                    run.record(trial, 'completed', result=result)
            self.assertEqual(len(fixture.commands), sent)
            run.record(trial, 'completed', result={'rt': None})
            self.assertEqual(session.results, [dict(trial_id='trial-1', status='completed', result={'rt': None})])

    def test_session_results_remain_after_every_trial_is_finished(self):
        fixture = Fixture(two_runs())
        with server(fixture) as (url, _):
            session = Client('demo', 'secret', url=url).experiment('Demo').subjects[0].sessions[0]
            with session:
                while (run := session.next_run()) is not None:
                    while (trial := run.next_trial()) is not None:
                        run.record(trial, 'started')
                        run.record(trial, 'completed', result={'rt': trial.position})
            self.assertIsNone(session.next_run())
            self.assertEqual([row['trial_id'] for row in session.results],
                             ['run1-trial-1', 'run1-trial-2', 'run2-trial-1', 'run2-trial-2'])
            self.assertEqual(session.run('run-002').results, session.results[2:])
            self.assertEqual(finishes(fixture), ['completed'])

    def test_objects_display_without_requests(self):
        with server(Fixture(two_runs())) as (url, requests):
            client = Client('demo', 'secret', url=url)
            experiment = client.experiment('Demo')
            subject = experiment.subjects[0]
            session = subject.sessions[0]
            before = len(requests)
            self.assertEqual([repr(item) for item in (client, experiment, client.experiment(id='experiment-1'),
                                                      subject, session, session.run('run-002'))],
                             [f'<Client demo at {url}>', "<Experiment 'Demo' experiment-1>", '<Experiment experiment-1>',
                              '<Subject subject-001: 1 session>', '<Session session-001 (subject-001): run-001, run-002>',
                              '<Run run-002 (session-001): 2 trials>'])
            self.assertEqual(len(requests), before)
            with self.assertRaisesRegex(ValueError, "No run 'run-003' in session-001; choose one of run-001, run-002"):
                session.run('run-003')
            with self.assertRaisesRegex(ValueError, "No session 'session-002' for subject-001; choose one of session-001"):
                subject.session('session-002')

    def test_start_names_explicit_takeover_while_another_program_holds_the_session(self):
        with server(Fixture()) as (url, _):
            experiment = Client('demo', 'secret', url=url).experiment('Demo')
            experiment.subjects[0].sessions[0].start()
            other = Client('demo', 'secret', url=url).experiment('Demo').subjects[0].sessions[0]
            with self.assertRaisesRegex(ApiError, r'HTTP 409: Another presenter owns this session.*If that program '
                                                  r'has stopped, call session.start\(takeover=True\)'):
                other.start()
            other.start(takeover=True)
            with other:
                self.assertEqual(other.next_run().next_trial().trial_id, 'trial-1')

    def test_rounded_shared_fraction_is_reported(self):
        fixture = Fixture()

        def respond(method, path, headers, body):
            if path == '/api/v1/experiments' and method == 'POST':
                return response(dict(id='experiment-1', settings=dict(parents_per_subject=6, shared_per_subject=0)), 201)
            return fixture(method, path, headers, body)

        with server(respond) as (url, _):
            client = Client('demo', 'secret', url=url)
            with self.assertWarnsRegex(UserWarning, r'shared_fraction=0.1 rounds down to 0 of 6 parent videos per '
                                                    r'subject \(realized fraction 0\)'):
                client.create_experiment('Demo', subjects=1, trials_per_run=6, shared_fraction=0.1)
            with patch('dbstim.client.warnings.warn') as warned:
                client.create_experiment('Demo', subjects=1, trials_per_run=6)
            warned.assert_not_called()


if __name__ == '__main__':
    unittest.main()
