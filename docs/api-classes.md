# API objects

Start with `dbstim.connect()`, which asks for your password and returns a
`Client` signed in to **dbstimuli.com**; `url=` selects another server. Arrows below show how you obtain each object.

```mermaid
classDiagram
    direction TB
    class Client {
        username
        url
        metrics() list_of_Metric
        experiments()
        create_experiment(name, settings) Experiment
        experiment(name_or_id) Experiment
    }
    class Experiment {
        id
        summary()
        subjects
        publish() Experiment
        subject(id) Subject
    }
    class Subject {
        id
        sessions
        session_ids
        session(id, directory) Session
    }
    class Session {
        id
        run_ids
        with session
        next_run() Run_or_None
        download(directory) Path
        run(id) Run
        start(takeover)
        finish(status)
    }
    class Run {
        id
        trials
        results
        next_trial() Trial_or_None
        media(trial, mode) Path_or_URL
        record(trial, status, result)
    }
    class Trial {
        <<frozen dataclass>>
        trial_id
        stimulus_id
        position
        role
        media_type
        segment
    }
    class Segment {
        <<frozen dataclass>>
        start_seconds
        end_seconds
    }
    class MetricFilter {
        <<frozen dataclass>>
        metric_id
        operator
        value
        to_dict()
    }
    Client --> Experiment : create or load
    Experiment --> Subject : subjects
    Subject --> Session : sessions
    Session --> Run : next_run
    Run --> Trial : trials or next_trial
    Trial --> "0..1" Segment : source interval
    Client ..> MetricFilter : creation filters
```

Signatures are abbreviated: creation settings are keyword arguments, and lookup
uses `experiment("Exact name")` or `experiment(id="revision-id")`.
Name lookup prefers the latest published revision; ambiguous names require an ID.
Use `Client.close()` or a `with` block when finished.

## What the objects mean

- **Experiment:** saved selection and design; publishing freezes assignments.
- **Subject:** assigned sessions from a published, hash-verified manifest.
- **Session:** download cache and presentation claim. `with session:` starts it
  and finishes `completed`, `interrupted`, or `failed`; `finish(status)` does so by hand.
- **Run:** ordered trials; `next_trial()` reads server progress. `results` is a
  property containing recorded results, not a method.
- **Trial:** immutable assignment, not a player or progress record. Positions
  start at one within each run; roles are `parent`, `repeat`, or `foil`.
- **Segment:** optional source interval. Initial halves and complementary foils
  are already rendered for playback from time zero.
- **MetricFilter:** metric, comparison operator (`">="` or `"gte"`), and threshold.
- **Metric:** one filterable metric from `client.metrics()`.

`session.next_run()` returns the first run with unfinished trials, or `None`.
`session.run("latest")` selects the most recently started run instead. An
unconfirmed start in any run blocks `next_trial()` for the whole session until
reviewed. `run.media(trial)` returns a verified local file, downloading it first
if needed.

Workflow classes live in `src/dbstim/workflow.py`, authentication in `client.py`,
and frozen data types in `models.py`. Video is currently the supported modality.
PGL/PsychoPy owns presentation; **the server owns progress**.

[Metric storage and filters](metrics-and-filters.md) · [Media and session flow](server-to-local-flow.md)
