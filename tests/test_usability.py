from contextlib import nullcontext
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
        with self.assertRaises(ValueError):
            MetricFilter('duration_seconds', '=>', 10)

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
        self.assertEqual(repr(metric), '<Metric duration_seconds: Duration (number, seconds); operators gt gte>')

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
                            run.record(trial, 'completed')
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
        with server(fixture) as (url, _):
            session = Client('demo', 'secret', url=url).experiment('Demo').subjects[0].sessions[0]
            with self.assertRaises(KeyError) as caught:
                with session:
                    run = session.next_run()
                    fixture.lost_reply = True
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


if __name__ == '__main__':
    unittest.main()
