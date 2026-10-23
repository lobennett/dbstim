"""Opt-in fixture: the real dbp-dataset-browser code on temporary databases over localhost HTTP.

Set DBSTIM_SERVER to a server checkout, and DBSTIM_SERVER_PYTHON to an interpreter with
its requirements-web.txt packages when this one lacks them. Nothing contacts a deployed server.
"""

from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading

CHECKOUT = os.environ.get('DBSTIM_SERVER', '')
PYTHON = os.environ.get('DBSTIM_SERVER_PYTHON', sys.executable)
ACCOUNTS = dict(admin='test-password-123', other='test-password-456')


@contextmanager
def real_server(*, legacy_events: bool = False):
    """Yield (origin, data directory) for a freshly built server; stop and delete it afterwards."""
    command = [PYTHON, str(Path(__file__).with_name('serve_real_server.py')), CHECKOUT]
    process = subprocess.Popen(command + ['--legacy-events'] * legacy_events, stdout=subprocess.PIPE, text=True)
    try:
        ready = json.loads(process.stdout.readline() or '{}')
        if 'port' not in ready:
            raise RuntimeError('The server fixture did not start; check DBSTIM_SERVER and DBSTIM_SERVER_PYTHON')
        yield f"http://127.0.0.1:{ready['port']}", Path(ready['root'])
    finally:
        process.terminate()
        process.wait(timeout=30)
        process.stdout.close()


def count(root: Path, table: str) -> int:
    """Rows in one of the server's append-only tables, read directly from its database."""
    with closing(sqlite3.connect(f"file:{root / 'accounts.sqlite3'}?mode=ro", uri=True)) as database:
        return database.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]  # noqa: S608 - test-chosen table


def _read_message(stream) -> bytes:
    head = b''
    while b'\r\n\r\n' not in head:
        chunk = stream.recv(65536)
        if not chunk:
            return head
        head += chunk
    headers, _, body = head.partition(b'\r\n\r\n')
    length = next((int(line.split(b':', 1)[1]) for line in headers.split(b'\r\n')
                   if line.lower().startswith(b'content-length:')), 0)
    while len(body) < length:
        chunk = stream.recv(65536)
        if not chunk:
            break
        body += chunk
    return headers + b'\r\n\r\n' + body


class FaultProxy:
    """Relays one request per connection; queued faults lose a request or its reply.

    Faults are (method, path suffix, kind). Kind "request" closes the connection before
    the server sees the request, "response" lets the server process it and then closes
    the connection without the reply, and an integer status replies with that status
    instead ("request") or after the server processed it (passed as "response:503").
    seen records (method, path, kind, body) for every request the proxy received.
    """

    def __init__(self, origin: str):
        self.upstream = ('127.0.0.1', int(origin.rsplit(':', 1)[1]))
        self.faults: list[tuple[str, str, str]] = []
        self.seen: list[tuple[str, str, str, bytes]] = []
        self._listener = socket.create_server(('127.0.0.1', 0))
        self.origin = f'http://127.0.0.1:{self._listener.getsockname()[1]}'
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._listener.close()

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._relay, args=(client,), daemon=True).start()

    def _relay(self, client: socket.socket) -> None:
        with client:
            request = _read_message(client)
            method, path = request.split(b' ', 2)[:2] if request.count(b' ') >= 2 else (b'', b'')
            method, path = method.decode(), path.decode()
            fault = next((item for item in self.faults if item[0] == method and path.endswith(item[1])), None)
            if fault is not None:
                self.faults.remove(fault)
            kind = fault[2] if fault else 'relay'
            self.seen.append((method, path, kind, request.partition(b'\r\n\r\n')[2]))
            if kind == 'request':
                return
            if kind.isdigit():
                client.sendall(_status(int(kind)))
                return
            with socket.create_connection(self.upstream) as upstream:
                upstream.sendall(request)
                reply = b''
                while chunk := upstream.recv(65536):
                    reply += chunk
            if kind == 'response':
                return
            client.sendall(_status(int(kind.split(':')[1])) if kind.startswith('response:') else reply)


def _status(code: int) -> bytes:
    body = json.dumps(dict(detail='Injected server fault; retry shortly.')).encode()
    return (f'HTTP/1.1 {code} Injected\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n'
            'Connection: close\r\n\r\n').encode() + body
