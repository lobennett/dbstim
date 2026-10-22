"""Typed views of immutable assignments and server-owned presentation state."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import TYPE_CHECKING
import uuid

from ._transport import ApiError
from .models import JSONObject, MediaMode, Segment, SessionStatus, Trial, TrialStatus, identifier

if TYPE_CHECKING:
    from .client import Client


def _hash_file(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


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
    return [trial for trial in trials if state['progress'].get(trial.trial_id, {}).get('status') != 'completed']


class Experiment:
    def __init__(self, client: Client, experiment_id: str):
        self._client = client
        self.id = identifier(experiment_id)
        self._path = '/api/v1/experiments/' + self.id

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

    def session(self, id: str = 'session-001', *, directory: str | Path | None = None) -> Session:
        if id not in self.session_ids:
            raise ValueError('Choose a session from subject.session_ids')
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
        self._pending: tuple[str, JSONObject] | None = None
        self._blocks = tuple(block for block in subject._blocks if block.session_id == self.id)
        self.run_ids = tuple(block.run_id for block in self._blocks)
        self._directory = self._root(Path(directory)) if directory is not None else None

    def _state(self) -> JSONObject:
        if self._pending is not None:
            raise ApiError('A request has no acknowledgment. Retry the same operation before continuing.')
        state = self._http._json('GET', self._path)
        if state['manifest_sha256'] != self.subject._manifest['manifest_sha256']:
            raise ApiError('Session manifest changed')
        return state

    def _send(self, action: str, **values) -> None:
        key = json.dumps([action, values], sort_keys=True, allow_nan=False)
        if self._pending is not None and self._pending[0] != key:
            raise ApiError('Retry the unacknowledged operation first')
        body = self._pending[1] if self._pending else dict(request_id=uuid.uuid4().hex, writer_id=self._writer,
                                                         **json.loads(json.dumps(values, allow_nan=False)))
        self._pending = (key, body)
        try:
            acknowledgment = self._http._json('POST', self._path + '/' + action, body)
        except ApiError as error:
            if error.status is not None and 400 <= error.status < 500:
                self._pending = None
            raise
        if acknowledgment.get('accepted') != body['request_id']:
            raise ApiError('Server did not acknowledge this command')
        self._pending = None

    def start(self, *, takeover: bool = False) -> None:
        self._send('start', takeover=takeover)

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
        elif error is not None or _unfinished(self._state(), tuple(trial for block in self._blocks
                                                                   for trial in block.trials)):
            status = 'interrupted'
        else:
            status = 'completed'
        try:
            self.finish(status)
        except ApiError as failure:
            if error is None:
                raise
            error.add_note(f'The session could not be marked {status}: {failure}')

    def run(self, id: str = 'latest') -> Run:
        if id == 'latest':
            id = self._state()['latest_run_id']
        if id not in self.run_ids:
            raise ValueError('Choose a run from session.run_ids')
        return Run(self, id, self._blocks[self.run_ids.index(id)])

    def next_run(self) -> Run | None:
        """The first run with unfinished trials, or None when every trial is completed."""
        state = self._state()
        return next((Run(self, block.run_id, block) for block in self._blocks if _unfinished(state, block.trials)), None)

    def download(self, directory: str | Path) -> Path:
        self._directory = self._root(Path(directory))
        for block in self._blocks:
            for trial in block.trials:
                self._media(trial, 'cache')
        return self._directory

    def _root(self, directory: Path) -> Path:
        if directory.is_symlink():
            raise ValueError('Download directory must not be a symlink')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory.resolve()

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
        if self._directory is None:
            origin = hashlib.sha256(self._http.origin.encode()).hexdigest()[:16]
            self._directory = self._root(Path('.dbstim') / origin / self.subject.experiment.id /
                                         self.subject.id / self.subject._manifest['manifest_sha256'] / self.id)
        root = self._directory
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
            raise ApiError('Download this session first, or choose mode="cache"')
        with self._http._request('GET', endpoint + '/media', media=True) as response:
            expected = response.headers.get('X-Content-SHA256', '')
            size = response.headers.get('Content-Length', '')
            if (response.headers.get_content_type() != 'video/mp4' or not re.fullmatch('[0-9a-f]{64}', expected)
                    or not size.isdigit() or not 0 < int(size) <= self._http.max_media_bytes):
                raise ApiError('Media response is missing a valid size, type or checksum')
            digest = hashlib.sha256()
            written = 0
            with tempfile.TemporaryDirectory(dir=root) as temporary:
                partial = Path(temporary) / 'media.mp4'
                with partial.open('wb') as stream:
                    while chunk := response.read(min(1024 * 1024, int(size) - written + 1)):
                        written += len(chunk)
                        if written > int(size):
                            raise ApiError('Media exceeds declared size')
                        digest.update(chunk)
                        stream.write(chunk)
                if written != int(size) or digest.hexdigest() != expected:
                    raise ApiError('Media checksum or size mismatch')
                metadata = Path(temporary) / 'receipt.json'
                metadata.write_text(json.dumps(dict(identity, sha256=expected, size_bytes=written)))
                for source, destination in ((partial, target), (metadata, receipt)):
                    try:
                        os.link(source, destination)
                    except FileExistsError:
                        if destination.is_symlink() or _hash_file(destination) != _hash_file(source):
                            raise ApiError('Existing download conflicts with the verified media or receipt') from None
        return target


class Run:
    def __init__(self, session: Session, run_id: str, block: _Block):
        self.session = session
        self.id = run_id
        self.trials = block.trials

    @property
    def results(self) -> list[JSONObject]:
        trial_ids = {trial.trial_id for trial in self.trials}
        return [result for result in self.session._state()['results'] if result['trial_id'] in trial_ids]

    def next_trial(self) -> Trial | None:
        state = self.session._state()
        if state['unconfirmed']:
            raise ApiError('An unconfirmed trial requires review; no automatic replay or skip')
        return next(iter(_unfinished(state, self.trials)), None)

    def media(self, trial: Trial, mode: MediaMode = 'cache') -> Path | str:
        if trial not in self.trials:
            raise ValueError('Choose a trial from this run')
        return self.session._media(trial, mode)

    def record(self, trial: Trial, status: TrialStatus, result: JSONObject | None = None) -> None:
        if trial not in self.trials or status not in ('started', 'completed'):
            raise ValueError('Choose a trial from this run and started or completed')
        if result is not None and not isinstance(result, dict):
            raise ValueError('result must be a JSON object or None')
        self.session._send('trials', trial_id=trial.trial_id, status=status, result=result)
