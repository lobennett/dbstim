"""Authenticated entry point for the stimulus browser."""

from collections.abc import Sequence
from datetime import datetime
from getpass import getpass
import math
import warnings

from ._transport import ApiError, Transport
from .models import JSONObject, Metric, MetricFilter, identifier
from .workflow import Experiment


def connect(username: str | None = None, *, url: str = 'https://dbstimuli.com', timeout: float = 120) -> 'Client':
    """Sign in, asking for the username (if not given) and the password without echoing it."""
    if username is None:
        username = input('dbstimuli.com username: ').strip()
    return Client(username, getpass('dbstimuli.com password: '), url=url, timeout=timeout)


def _when(timestamp) -> str:
    if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
        return 'at an unknown time'
    return datetime.fromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M')


class Client:
    def __init__(self, username: str, password: str, *, url: str = 'https://dbstimuli.com', timeout: float = 120):
        self._http = Transport(url, timeout=timeout)
        self.username = self._http.login(username, password)['user']['username']
        self.url = self._http.origin

    def __repr__(self) -> str:
        return f'<Client {self.username} at {self.url}>'

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
            if not matches:
                raise ValueError(f'No experiment is named {name!r}; client.experiments() lists yours')
            if len(matches) > 1:
                # Names may repeat (each create makes a new experiment); show the ids to choose from.
                choices = '; '.join(f"{row.get('latest_published_revision_id') or row['id']} "
                                    f"({'published' if row.get('latest_published_revision_id') else 'draft'}, "
                                    f"created {_when(row.get('created_at'))})" for row in matches)
                raise ValueError(f'{len(matches)} experiments are named {name!r}: {choices}. '
                                 'Open one with client.experiment(id=...)')
            id = matches[0].get('latest_published_revision_id') or matches[0]['id']
        return Experiment(self, identifier(id), _name=name)

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
        saved = document.get('settings')
        shared, total = (saved.get('shared_per_subject'), saved.get('parents_per_subject')) if isinstance(saved, dict) else (None, None)
        if type(shared) is int and type(total) is int and total > 0 and shared / total != shared_fraction:
            warnings.warn(f'shared_fraction={shared_fraction} rounds down to {shared} of {total} parent videos '
                          f'per subject (realized fraction {shared / total:.3g})', stacklevel=2)
        return Experiment(self, identifier(document['id']), _name=name)
