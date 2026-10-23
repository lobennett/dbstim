# Recovering a session

The server owns progress, so recovery starts from what it recorded. Each trial
is recorded `started`, then ends in exactly one way:

- `completed`: shown to the end, optionally with a result.
- `abandoned`: started but not shown to the end (Ctrl-C, a crash, a power cut).

Neither ending is presented again. A started trial with no ending is
*unconfirmed*, and it blocks the session until you record one.

## A program stopped mid-trial

`run.next_trial()` then names the trial, for example `trial-000002 (run-001,
position 2) was started but neither completed nor abandoned`. Resolve it and
continue with the usual loop:

```python
with session:
    for trial in session.unconfirmed:
        session.record(trial, "abandoned")  # or "completed", result=... if it was shown
    while (run := session.next_run()) is not None:
        ...  # the next trial follows the abandoned one
```

Record `completed` only for a trial the participant really saw. A session with an
abandoned trial finishes `interrupted`, never `completed`. `session.abandoned`
lists every abandoned trial, even one recorded without a result;
`session.results` holds only the results you sent, each with its status.

## Results

A result is plain JSON: strings, numbers, `True`/`False`, `None`, lists and
dicts. `numpy.int64(3)` or `float("nan")` raise `ValueError` before anything is
sent; convert with `int()` or `float()` and use `None` for no response.

## Dropped connections

`start`, `finish` and `record` each send a request ID. When the connection fails
or the server answers 500, 502, 503 or 504, dbstim resends the identical command
up to three times, pausing 0.5, 1 and 2 seconds. The server recognizes the ID, so
nothing is recorded twice. Client errors (4xx) are never resent. If every attempt
fails, the error names the call: repeat that same call on the same session object
before anything else.

## Another presenter owns the session

A program that crashed or lost its connection can still hold the session. Once
you are sure it has stopped, take over explicitly, then resolve any unconfirmed
trial as above:

```python
session.start(takeover=True)
with session:
    ...
```

Never take over a session that another computer is still presenting.

## Media cache

Verified videos live in `$XDG_CACHE_HOME/dbstim` or `~/.cache/dbstim`, separated
by server, experiment, subject, manifest and session. Every program reuses them
after checking size, SHA-256 and identity; a changed file is refused, not
replaced. Disk failures are reported as disk errors: drives that cannot hard-link
files, such as exFAT, need another folder via `session.download(directory)`.

## Upgrading from 0.4

- dbstim 0.4 kept downloads in `.dbstim` inside the working folder. 0.5 does not
  read it, so each session downloads once more. Delete old `.dbstim` folders
  yourself when convenient.
- Recording `abandoned` needs a server with abandoned outcomes enabled
  (dbp-dataset-browser 492a894 or later, after its operator upgrades the
  database). Until then the server refuses the call, nothing changes and the
  trial stays unconfirmed.
- dbstim 0.4 cannot move past an abandoned trial: it offers the trial again and
  the server refuses to present it. Use 0.5 for such sessions.
