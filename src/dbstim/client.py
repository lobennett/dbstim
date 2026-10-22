"""Authenticated entry point for the stimulus browser."""

from collections.abc import Sequence
from getpass import getpass
import math

from ._transport import ApiError, Transport
from .models import JSONObject, Metric, MetricFilter, identifier
from .workflow import Experiment


def connect(username: str | None = None, *, url: str = 'https://dbstimuli.com', timeout: float = 120) -> 'Client':
    """Sign in, asking for the username (if not given) and the password without echoing it."""
    if username is None:
        username = input('dbstimuli.com username: ').strip()
    return Client(username, getpass('dbstimuli.com password: '), url=url, timeout=timeout)


class Client:
    def __init__(self, username: str, password: str, *, url: str = 'https://dbstimuli.com', timeout: float = 120):
        self._http = Transport(url, timeout=timeout)
        self.username = self._http.login(username, password)['user']['username']
        self.url = self._http.origin

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> 'Client':
        return self

    def __exit__(self, *args):
        self.close()

    def metrics(self) -> list[Metric]:
        """Metrics you can filter on, such as duration_seconds."""
        try:
            return [Metric(row['id'], row.get('label') or row['id'], row.get('kind', ''), row.get('unit'),
                           tuple(row.get('operators', ())))
                    for row in self._http._json('GET', '/api/v1/metrics')['metrics']]
        except (TypeError, KeyError, AttributeError) as error:
            raise ApiError('Invalid metric list') from error

    def experiments(self) -> list[JSONObject]:
        rows = []
        while True:
            page = self._http._json('GET', f'/api/v1/experiments?grouped=true&limit=100&offset={len(rows)}')['experiments']
            rows.extend(page)
            if len(page) < 100:
                return rows

    def experiment(self, name: str | None = None, *, id: str | None = None) -> Experiment:
        if (name is None) == (id is None):
            raise ValueError('Provide an exact experiment name or id')
        if id is None:
            matches = [row for row in self.experiments() if row['name'] == name]
            if len(matches) != 1:
                raise ValueError('Experiment name not found or ambiguous; choose an id from client.experiments()')
            id = matches[0].get('latest_published_revision_id') or matches[0]['id']
        return Experiment(self, identifier(id))

    def create_experiment(self, name: str, *, subjects: int, trials_per_run: int,
                          sessions_per_subject: int = 1, runs_per_session: int = 1,
                          foils_per_run: int = 0, repeats_per_run: int = 0, shared_fraction: float = 0,
                          seed: str | int = '0', sampling: str = 'random', filters: Sequence[MetricFilter] = (),
                          content: str = '', corpus: str = 'both') -> Experiment:
        counts = dict(subject_count=subjects, sessions_per_subject=sessions_per_subject,
                      runs_per_session=runs_per_session, trials_per_run=trials_per_run,
                      foils_per_run=foils_per_run, repeats_per_run=repeats_per_run)
        for key, value in counts.items():
            minimum = 0 if key in ('foils_per_run', 'repeats_per_run') else 1
            if type(value) is not int or not minimum <= value <= 50000:
                raise ValueError(f'{key} must be an integer between {minimum} and 50000')
        if subjects > 100 or subjects * sessions_per_subject * runs_per_session * trials_per_run > 50000:
            raise ValueError('Maximum 100 subjects and 50,000 total presentations')
        if (type(shared_fraction) not in (int, float) or not math.isfinite(shared_fraction)
                or not 0 <= shared_fraction <= 1):
            raise ValueError('shared_fraction must be between 0 and 1')
        parents = trials_per_run - foils_per_run - repeats_per_run
        if parents <= 0 or max(foils_per_run, repeats_per_run) > parents:
            raise ValueError('Foils and repeats require enough initial presentations in every run')
        if type(seed) is int:
            seed = str(seed)
        if not isinstance(seed, str) or not seed.strip() or not isinstance(name, str) or not name.strip():
            raise ValueError('name must be a nonempty string and seed a nonempty string or integer')
        if sampling not in ('random', 'balanced', 'unique') or corpus not in ('captions', 'transcripts', 'both'):
            raise ValueError('Choose a supported channel sampling method and caption/transcript corpus')
        query = dict(filters=[item.to_dict() for item in filters], content_query=content, corpus=corpus,
                     mode='keyword', custom_metrics=[])
        version = self._http._json('POST', '/api/v1/media/query', dict(query, limit=1))['version']
        settings = dict(counts, name=name, seed=seed, shared_fraction=shared_fraction, channel_sampling=sampling)
        document = self._http._json('POST', '/api/v1/experiments', dict(query, version=version, settings=settings))
        return Experiment(self, identifier(document['id']))
