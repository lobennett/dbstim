"""Typed views of immutable assignments and server-owned presentation state."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
from functools import cached_property
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import TYPE_CHECKING
import uuid

from ._transport import ApiError, _Unreachable
from .models import JSONObject, MediaMode, Segment, SessionStatus, Trial, TrialStatus, identifier

if TYPE_CHECKING:
    from .client import Client

# A started trial ends "completed" (shown to the end) or "abandoned" (not); neither is presented again.
_RESOLVED = ('completed', 'abandoned')
# Pauses before resending an unacknowledged session command; the server deduplicates its request ID.
_RETRY_DELAYS = (0.5, 1.0, 2.0)
_RETRY_STATUSES = (500, 502, 503, 504)
_NO_HARD_LINKS = (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP)


def _hash_file(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _cache_home() -> Path:
    """The per-user media cache every program shares: $XDG_CACHE_HOME/dbstim or ~/.cache/dbstim."""
    configured = os.environ.get('XDG_CACHE_HOME', '')
    return (Path(configured) if os.path.isabs(configured) else Path.home() / '.cache') / 'dbstim'


@contextmanager
def _saving(root: Path):
    """Report a local disk failure as one, never as a connection failure."""
    try:
        yield
    except OSError as error:
        raise OSError(error.errno, f'Could not save media in {root}: {error.strerror or error}') from error


@dataclass(frozen=True)
class _Block:
    session_id: str
    run_id: str
    trials: tuple[Trial, ...]


def _manifest(document: JSONObject, experiment_id: str, subject_id: str) -> tuple[_Block, ...]:
    payload = {key: value for key, value in document.items() if key != 'manifest_sha256'}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'),
                                      ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    if (document.get('schema_version') != 'dbp-experiment-v1' or document.get('experiment_id') != experiment_id
            or document.get('subject_id') != subject_id or document.get('manifest_sha256') != digest):
        raise ApiError('Manifest identity or SHA-256 does not match')
    seen = set()
    parsed: list[_Block] = []
    blocks = document.get('blocks')
    if not isinstance(blocks, list) or not blocks:
        raise ApiError('Manifest requires runs')
    for index, block in enumerate(blocks, 1):
        if (not isinstance(block, dict) or not isinstance(block.get('trials'), list)
                or type(block.get('block_index')) is not int or block['block_index'] != index):
            raise ApiError('Invalid manifest block order')
        try:
            session_id = identifier(block.get('session_id', 'session-001'))
            run_id = identifier(block.get('run_id', f'run-{index:03d}'))
        except ValueError as error:
            raise ApiError('Invalid manifest session or run identity') from error
        if any((item.session_id, item.run_id) == (session_id, run_id) for item in parsed):
            raise ApiError('Duplicate run identity within a session')
        trials = tuple(_trial(row, position) for position, row in enumerate(block['trials'], 1))
        for trial in trials:
            if trial.trial_id in seen:
                raise ApiError('Duplicate trial identity')
            seen.add(trial.trial_id)
        parsed.append(_Block(session_id, run_id, trials))
    if not seen:
        raise ApiError('Manifest has no trials')
    return tuple(parsed)


def _trial(row: JSONObject, position: int) -> Trial:
    try:
        segment = Segment(**row['segment']) if row['segment'] is not None else None
        return Trial(row['trial_id'], row['media_id'], position, row['role'], row['media_type'], segment)
    except (ValueError, TypeError, KeyError) as error:
        raise ApiError('Invalid manifest trial') from error


def _unfinished(state: JSONObject, trials: tuple[Trial, ...]) -> list[Trial]:
    return [trial for trial in trials if state['progress'].get(trial.trial_id, {}).get('status') not in _RESOLVED]


def _label(action: str, values: JSONObject) -> str:
    if action == 'trials':
        return f"record({values['trial_id']}, {values['status']!r})"
    return f"{action}({', '.join(f'{key}={value!r}' for key, value in values.items())})"


class Experiment:
    def __init__(self, client: Client, experiment_id: str, *, _name: str | None = None):
        self._client = client
        self.id = identifier(experiment_id)
        self._path = '/api/v1/experiments/' + self.id
        self._name = _name

    def __repr__(self) -> str:
        return f'<Experiment {self._name!r} {self.id}>' if self._name else f'<Experiment {self.id}>'

    def summary(self) -> JSONObject:
        document = self._client._http._json('GET', self._path)
        settings = document['settings']
        parents = settings.get('parents_per_subject', 0)
        publication = document.get('publication')
        subject_ids = ([row['subject_id'] for row in publication['subjects']] if publication is not None
                       else [f'subject-{index + 1:03d}' for index in range(settings['subject_count'])])
        return dict(id=self.id, name=settings['name'], published=publication is not None,
                    settings=settings, shared_fraction_realized=settings.get('shared_per_subject', 0) / parents if parents else 0,
                    subject_ids=subject_ids)

    def publish(self) -> Experiment:
        self._client._http._json('POST', self._path + '/publish', {})
        return self

    def subject(self, id: str) -> Subject:
        return Subject(self, identifier(id))

    @cached_property
    def subjects(self) -> tuple[Subject, ...]:
        """Every subject of the published experiment, in the server's order."""
        publication = self._client._http._json('GET', self._path).get('publication')
        if publication is None:
            raise ApiError('Publish the experiment before choosing subjects')
        try:
            rows = [(identifier(row['subject_id']), row) for row in publication['subjects']]
        except (ValueError, TypeError, KeyError) as error:
            raise ApiError('Invalid published subject list') from error
        return tuple(Subject(self, subject_id, _document=row) for subject_id, row in rows)


class Subject:
    def __init__(self, experiment: Experiment, subject_id: str, *, _document: JSONObject | None = None):
        self.experiment = experiment
        self.id = subject_id
        self._path = experiment._path + '/subjects/' + self.id
        self._manifest = (_document if _document is not None
                          else experiment._client._http._json('GET', self._path + '/manifest'))
        self._blocks = _manifest(self._manifest, experiment.id, self.id)
        self.session_ids = tuple(dict.fromkeys(block.session_id for block in self._blocks))

    def __repr__(self) -> str:
        count = len(self.session_ids)
        return f"<Subject {self.id}: {count} session{'' if count == 1 else 's'}>"

    def session(self, id: str = 'session-001', *, directory: str | Path | None = None) -> Session:
        if id not in self.session_ids:
            raise ValueError(f"No session {id!r} for {self.id}; choose one of {', '.join(self.session_ids)}")
        return Session(self, id, directory=directory)

    @cached_property
    def sessions(self) -> tuple[Session, ...]:
        """This subject's sessions in assigned order; the same objects on every access."""
        return tuple(Session(self, session_id) for session_id in self.session_ids)


class Session:
    def __init__(self, subject: Subject, session_id: str, *, directory: str | Path | None = None):
        self.subject = subject
        self.id = identifier(session_id)
        self._http = subject.experiment._client._http
        self._path = subject._path + '/sessions/' + self.id
        self._writer = uuid.uuid4().hex
        self._pending: tuple[str, str, JSONObject] | None = None
        self._blocks = tuple(block for block in subject._blocks if block.session_id == self.id)
        self._trials = tuple(trial for block in self._blocks for trial in block.trials)
        self.run_ids = tuple(block.run_id for block in self._blocks)
        self._directory = self._root(Path(directory)) if directory is not None else None

    def __repr__(self) -> str:
        return f"<Session {self.id} ({self.subject.id}): {', '.join(self.run_ids)}>"

    def _state(self) -> JSONObject:
        if self._pending is not None:
            raise ApiError(f'{self._pending[1]} was not acknowledged; repeat that call to retry it unchanged '
                           'before continuing')
        state = self._http._json('GET', self._path)
        if state['manifest_sha256'] != self.subject._manifest['manifest_sha256']:
            raise ApiError('Session manifest changed')
        return state

    def _send(self, action: str, **values) -> None:
        key = json.dumps([action, values], sort_keys=True, allow_nan=False)
        if self._pending is not None and self._pending[0] != key:
            raise ApiError(f'{self._pending[1]} was not acknowledged; repeat that call to retry it unchanged first')
        if self._pending is None:
            body = dict(request_id=uuid.uuid4().hex, writer_id=self._writer,
                        **json.loads(json.dumps(values, allow_nan=False)))
            self._pending = (key, _label(action, values), body)
        label, body = self._pending[1:]
        # Resend the identical body only when the server may not have answered; never after a 4xx.
        for attempt, delay in enumerate((*_RETRY_DELAYS, None), 1):
            try:
                acknowledgment = self._http._json('POST', self._path + '/' + action, body)
                break
            except ApiError as error:
                if error.status is not None and 400 <= error.status < 500:
                    self._pending = None
                    raise
                if delay is not None and (isinstance(error, _Unreachable) or error.status in _RETRY_STATUSES):
                    time.sleep(delay)
                    continue
                raise ApiError(f"{error}; {label} was not acknowledged after {attempt} attempt{'s' * (attempt > 1)}. "
                               'Repeat that call to retry it unchanged before continuing', status=error.status) from None
        if acknowledgment.get('accepted') != body['request_id']:
            raise ApiError('Server did not acknowledge this command')
        self._pending = None

    def start(self, *, takeover: bool = False) -> None:
        """Claim presentation; takeover=True replaces a previous program, only once it has stopped."""
        try:
            self._send('start', takeover=takeover)
        except ApiError as error:
            if takeover or error.status != 409 or self._http._json('GET', self._path).get('status') != 'started':
                raise
            raise ApiError(f'{error} If that program has stopped, call session.start(takeover=True) '
                           'before presenting.', status=409) from None

    def finish(self, status: SessionStatus = 'completed') -> None:
        if status not in ('completed', 'interrupted', 'failed'):
            raise ValueError('Choose completed, interrupted or failed')
        self._send('finish', status=status)

    def __enter__(self) -> Session:
        self.start()
        return self

    def __exit__(self, kind, error, traceback) -> None:
        if isinstance(error, Exception):
            status = 'failed'
        elif error is not None:
            status = 'interrupted'
        else:
            # An abandoned trial is finished but not completed, so the session is interrupted.
            progress = self._state()['progress']
            completed = all(progress.get(trial.trial_id, {}).get('status') == 'completed' for trial in self._trials)
            status = 'completed' if completed else 'interrupted'
        try:
            self.finish(status)
        except ApiError as failure:
            if error is None:
                raise
            error.add_note(f'The session could not be marked {status}: {failure}')

    @property
    def unconfirmed(self) -> tuple[Trial, ...]:
        """Trials recorded started but neither completed nor abandoned, such as one cut off by Ctrl-C."""
        unconfirmed = set(self._state()['unconfirmed'])
        return tuple(trial for trial in self._trials if trial.trial_id in unconfirmed)

    @property
    def abandoned(self) -> tuple[Trial, ...]:
        """Every trial recorded abandoned, with or without a result; none is presented again."""
        abandoned = set(self._state().get('abandoned', ()))  # Servers before abandoned outcomes have none.
        return tuple(trial for trial in self._trials if trial.trial_id in abandoned)

    @property
    def results(self) -> list[JSONObject]:
        """Every result payload recorded in this session, with its status, also after every trial is finished."""
        return self._state()['results']

    def record(self, trial: Trial, status: TrialStatus, result: JSONObject | None = None) -> None:
        """Report a trial: "started" before presenting it, then "completed" or "abandoned" (not shown to the end)."""
        if trial not in self._trials or status not in ('started', 'completed', 'abandoned'):
            raise ValueError('Choose a trial from this session and started, completed or abandoned')
        if result is not None and not isinstance(result, dict):
            raise ValueError('result must be a JSON object or None')
        try:
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError('result must hold plain JSON values (str, int, float, bool, None, lists, dicts): '
                             'convert numpy numbers with int() or float() and use None for no response') from error
        try:
            self._send('trials', trial_id=trial.trial_id, status=status, result=result)
        except ApiError as error:
            if status != 'abandoned' or error.status != 400:
                raise
            raise ApiError(f'{error} This server may predate abandoned trials; ask its operator to upgrade it. '
                           f'{trial.trial_id} was not changed.', status=400) from None

    def _unresolved(self, trial_ids: list[str]) -> str:
        names = ', '.join(f'{trial.trial_id} ({block.run_id}, position {trial.position})'
                          for block in self._blocks for trial in block.trials if trial.trial_id in trial_ids)
        return (f"{names or ', '.join(trial_ids)} was started but neither completed nor abandoned. If it was shown "
                'to the end, call session.record(trial, "completed", result=...); otherwise '
                'session.record(trial, "abandoned"). session.unconfirmed lists it; it is never replayed automatically.')

    def run(self, id: str = 'latest') -> Run:
        if id == 'latest':
            id = self._state()['latest_run_id']
        if id not in self.run_ids:
            raise ValueError(f"No run {id!r} in {self.id}; choose one of {', '.join(self.run_ids)}")
        return Run(self, id, self._blocks[self.run_ids.index(id)])

    def next_run(self) -> Run | None:
        """The first run with unfinished trials, or None when every trial is completed or abandoned."""
        state = self._state()
        return next((Run(self, block.run_id, block) for block in self._blocks if _unfinished(state, block.trials)), None)

    def download(self, directory: str | Path | None = None) -> Path:
        """Fetch and verify this session's videos into the shared per-user cache, or into directory."""
        if directory is not None:
            self._directory = self._root(Path(directory))
        for trial in self._trials:
            self._media(trial, 'cache')
        return self._cache()

    def _root(self, directory: Path) -> Path:
        if directory.is_symlink():
            raise ValueError('Download directory must not be a symlink')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory.resolve()

    def _cache(self) -> Path:
        if self._directory is None:
            home = _cache_home()
            home.mkdir(parents=True, exist_ok=True, mode=0o700)
            origin = hashlib.sha256(self._http.origin.encode()).hexdigest()[:16]
            self._directory = self._root(home / origin / self.subject.experiment.id / self.subject.id /
                                         self.subject._manifest['manifest_sha256'] / self.id)
        return self._directory

    def _media(self, trial: Trial, mode: MediaMode) -> Path | str:
        endpoint = self.subject._path + '/trials/' + trial.trial_id
        if mode == 'remote':
            access = self._http._json('POST', endpoint + '/media-access', {})
            path = access.get('path', '')
            if not isinstance(path, str) or not re.fullmatch('/api/v1/media-access/[0-9a-f]{64}', path):
                raise ApiError('Server returned an unexpected media link')
            return self._http.origin + path
        if mode not in ('local', 'cache'):
            raise ValueError('Choose local, cache or remote')
        root = self._cache()
        target = root / (trial.trial_id + '.mp4')
        receipt = root / (trial.trial_id + '.json')
        identity = dict(origin=self._http.origin, manifest_sha256=self.subject._manifest['manifest_sha256'],
                        session_id=self.id, trial_id=trial.trial_id)
        if target.is_symlink() or receipt.is_symlink():
            raise ApiError('Cached media or receipt is a symlink')
        if target.exists() and receipt.exists():
            try:
                saved = json.loads(receipt.read_text())
                if (any(saved.get(key) != value for key, value in identity.items())
                        or saved['sha256'] != _hash_file(target) or saved['size_bytes'] != target.stat().st_size):
                    raise ValueError('Mismatch')
            except (OSError, ValueError, KeyError) as error:
                raise ApiError('Cached media does not match its verified receipt') from error
            return target
        if mode == 'local':
            raise ApiError('Download this session first with session.download() (using the same directory if '
                           'you chose one), or choose mode="cache"')
        with self._http._request('GET', endpoint + '/media', media=True) as response:
            expected = response.headers.get('X-Content-SHA256', '')
            size = response.headers.get('Content-Length', '')
            if (response.headers.get_content_type() != 'video/mp4' or not re.fullmatch('[0-9a-f]{64}', expected)
                    or not size.isdigit() or not 0 < int(size) <= self._http.max_media_bytes):
                raise ApiError('Media response is missing a valid size, type or checksum')
            digest = hashlib.sha256()
            written = 0
            with _saving(root), tempfile.TemporaryDirectory(dir=root) as temporary:
                partial = Path(temporary) / 'media.mp4'
                with partial.open('wb') as stream:
                    while chunk := self._http._read(response, min(1024 * 1024, int(size) - written + 1)):
                        written += len(chunk)
                        if written > int(size):
                            raise ApiError('Media exceeds declared size')
                        digest.update(chunk)
                        stream.write(chunk)
                if written != int(size) or digest.hexdigest() != expected:
                    raise ApiError('Media checksum or size mismatch')
                metadata = Path(temporary) / 'receipt.json'
                metadata.write_text(json.dumps(dict(identity, sha256=expected, size_bytes=written)))
                # A hard link publishes each verified file atomically and never replaces another writer's.
                for source, destination in ((partial, target), (metadata, receipt)):
                    try:
                        os.link(source, destination)
                    except FileExistsError:
                        if destination.is_symlink() or _hash_file(destination) != _hash_file(source):
                            raise ApiError('Existing download conflicts with the verified media or receipt') from None
                    except OSError as error:
                        if error.errno not in _NO_HARD_LINKS:
                            raise
                        raise OSError(error.errno, 'this drive cannot hard-link files (for example exFAT or FAT); '
                                      'choose a directory on your main disk') from error
        return target


class Run:
    def __init__(self, session: Session, run_id: str, block: _Block):
        self.session = session
        self.id = run_id
        self.trials = block.trials

    def __repr__(self) -> str:
        return f'<Run {self.id} ({self.session.id}): {len(self.trials)} trials>'

    @property
    def results(self) -> list[JSONObject]:
        trial_ids = {trial.trial_id for trial in self.trials}
        return [result for result in self.session._state()['results'] if result['trial_id'] in trial_ids]

    def next_trial(self) -> Trial | None:
        """The next trial to present, or None; refuses while a started trial is unresolved."""
        state = self.session._state()
        if state['unconfirmed']:
            raise ApiError(self.session._unresolved(state['unconfirmed']))
        return next(iter(_unfinished(state, self.trials)), None)

    def media(self, trial: Trial, mode: MediaMode = 'cache') -> Path | str:
        if trial not in self.trials:
            raise ValueError('Choose a trial from this run')
        return self.session._media(trial, mode)

    def record(self, trial: Trial, status: TrialStatus, result: JSONObject | None = None) -> None:
        """Report a trial of this run; see Session.record."""
        if trial not in self.trials:
            raise ValueError('Choose a trial from this run')
        self.session.record(trial, status, result)
