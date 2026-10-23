"""Serve the real dbp-dataset-browser app on temporary databases; see real_server.py.

Usage: python serve_real_server.py <server checkout> [--legacy-events]. Prints one JSON
line with the port and data directory, then serves 127.0.0.1 until terminated.
--legacy-events first creates the trial events table as servers before abandoned
outcomes did, like a database that no operator has upgraded yet.
"""

from contextlib import closing
import json
from pathlib import Path
import socket
import sqlite3
import sys
import unittest

checkout = Path(sys.argv[1]).resolve()
sys.path[:0] = [str(checkout), str(checkout / 'tests')]

import uvicorn  # noqa: E402
from metric_collection_routes import install_metric_collections  # noqa: E402
from metric_study_routes import install_metric_studies  # noqa: E402
import test_metric_routes  # noqa: E402

# The events table of dbp-dataset-browser fc44a66, before abandoned outcomes.
LEGACY_EVENTS = """CREATE TABLE experiment_trial_events (
    event_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiment_publications(experiment_id),
    subject_id TEXT NOT NULL, manifest_sha256 TEXT NOT NULL,
    trial_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('started', 'completed')),
    occurred_at REAL NOT NULL, received_at REAL NOT NULL,
    actor_id TEXT NOT NULL, payload TEXT NOT NULL
)"""

# The server's own test setup: temporary catalog, metric index and account databases,
# with accounts admin/test-password-123 and other/test-password-456.
case = unittest.TestCase()
test_metric_routes.MetricRoutesTests.setUp(case)
if '--legacy-events' in sys.argv[2:]:
    with closing(sqlite3.connect(case.root / 'accounts.sqlite3')) as database, database:
        database.execute(LEGACY_EVENTS)
media = case.root / 'media'
media.mkdir()
for index in range(4):
    (media / f'source_part_{index:08d}.mp4').write_bytes(f'synthetic video {index}\n'.encode() * 512)


def lookup(clip_ids):
    return [dict(clip_id=clip_id, source_id='source', status='added',
                 metadata=json.dumps(dict(clip_id=clip_id, source_id='source'))) for clip_id in clip_ids]


def video_path(clip_id):
    path = media / f'{clip_id}.mp4'
    return path if path.is_file() else None


install_metric_studies(case.app, case.service, case.store, lambda request: 'test', lookup,
                       runner_video_path=video_path)
install_metric_collections(case.app, case.service, case.store, lookup, lambda request: 'test')
listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen(64)
print(json.dumps(dict(port=listener.getsockname()[1], root=str(case.root))), flush=True)
try:
    uvicorn.Server(uvicorn.Config(case.app, log_level='warning')).run(sockets=[listener])
finally:
    case.doCleanups()
