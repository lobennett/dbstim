# dbstim

Create experiments on [dbstimuli.com](https://dbstimuli.com), retrieve assigned
videos, and report presentation progress from your own task program.

## Install

Requires Python 3.11+. No other dependencies.

    pip install "git+https://github.com/lobennett/dbstim.git@v0.4.0"

or with uv:

    uv add "dbstim @ git+https://github.com/lobennett/dbstim.git@v0.4.0"

Pin a release tag (or a commit) so your analysis stays reproducible.
For the example notebook, add the Jupyter extra:

    pip install "dbstim[jupyter] @ git+https://github.com/lobennett/dbstim.git@v0.4.0"

You sign in with your dbstimuli.com account; no Google Cloud access or API key is needed.

## Create and publish

```python
import dbstim
from dbstim import MetricFilter

client = dbstim.connect()  # asks for your username and password
client.metrics()           # what you can filter on

experiment = client.create_experiment(
    "Memory demo", subjects=5, runs_per_session=2, trials_per_run=24,
    foils_per_run=4, shared_fraction=0.1, seed=1,
    filters=[MetricFilter("duration_seconds", ">=", 10),
             MetricFilter("duration_seconds", "<=", 20)],
)
experiment.publish()
```

To reopen one later: `client.experiment("Memory demo")`, or
`client.experiment(id="exact-revision-id")` when names are ambiguous.

## Present

```python
session = experiment.subjects[0].sessions[0]

with session:  # starts the session and finishes it when the block ends
    while (run := session.next_run()) is not None:
        while (trial := run.next_trial()) is not None:
            path = run.media(trial)  # verified local file; downloads if needed
            run.record(trial, "started")
            result = present(path)   # your PGL, PsychoPy, or other function
            run.record(trial, "completed", result=result)
```

Leaving the block finishes the session as `completed` when every trial is
completed, `interrupted` if you stopped early (including Ctrl-C), or `failed` if an
error was raised.
Stop on any API error and retry an unacknowledged call unchanged. A trial that
started but never completed blocks continuation until reviewed; it is never
replayed automatically.

`session.download(directory)` fetches the whole session ahead of time;
`run.media(trial, mode="remote")` returns a five-minute streaming URL instead.
Progress lives on the server, so another device sees the same state.
`run.results` reads the results you recorded.

The step-by-step notebook is `notebooks/api.ipynb`
(`uv run --extra jupyter jupyter lab notebooks/api.ipynb` from a clone).

## Glossary

- **Subject:** one participant's fixed assignment of videos.
- **Session / run:** a sitting, and a block of trials within it.
- **Trial:** one video presentation.
- **Parent:** a full video, or the first half of a video split for a foil.
- **Foil:** the second half of a split parent.
- **Repeat:** a parent shown a second time. Foils and repeats count toward `trials_per_run`.
- **shared_fraction:** share of parent videos every subject sees.
- **sampling:** how YouTube channels are drawn: `"random"`, `"balanced"`, or `"unique"`.
- **corpus:** searches `content` in `"captions"`, `"transcripts"`, or `"both"`.
- **Unconfirmed:** recorded `started` but never `completed`.
- **Takeover:** `session.start(takeover=True)` replaces another device's claim.

Older `dbp_api` clients are a legacy package pinned at revision `d11933f`; new
code should use `dbstim`.

## Development

```bash
uv run python -m unittest discover -s tests -v
```

[Objects](docs/api-classes.md) · [Data flow](docs/server-to-local-flow.md)
