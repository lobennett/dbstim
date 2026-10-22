# From experiment to presentation

## 1. Assignments and media

Creation saves a filtered sample and design. Publishing freezes each subject's
trial order, runs, and source intervals. Loading a subject verifies the manifest
hash; downloading or previewing leaves presentation progress unchanged.

```mermaid
flowchart TB
    subgraph Server["dbstimuli.com"]
        Catalog[(Catalog and metrics)] --> Draft["Draft experiment: filters, seed, design"]
        Draft -->|"publish()"| Manifest["Fixed subject / session / run assignments"]
        Manifest --> API["Authenticated API"]
        API <-->|"read / write"| Progress[(Presentation progress and results)]
    end
    GCP[(Private GCP media)] -->|"pinned full videos or rendered halves"| API
    subgraph Computer["Researcher's computer"]
        SDK["Client → Experiment → Subject → Session → Run"]
        Local["Verified MP4 cache + receipts"]
        Remote["Temporary playback URL"]
        Task["Your task: PGL / PsychoPy / another player"]
        SDK -->|"download() or media()"| Local
        SDK -->|"media(mode=remote)"| Remote
        Local --> Task
        Remote -->|"stream from website"| Task
        Task -->|"start / record / finish"| SDK
    end
    API -->|"assignments, media, progress"| SDK
    SDK -->|"authenticated requests"| API
```

`session.download(directory)` fetches the session's media; `run.media(trial)`
fetches one item if it is not already verified locally. `mode="local"` requires
verified files in the session directory; pass that same `directory=` when reopening. Files are checked
against size, SHA-256, and assignment identity. `mode="remote"` returns a five-minute
streaming URL. Researchers authenticate to the website, not Google Cloud.

## 2. Record and resume

Your task explicitly reports each presentation. The server records acknowledgments;
it cannot infer that someone watched a clip from a download.

```mermaid
sequenceDiagram
    actor Task as Your task program
    participant SDK as dbstim
    participant API as Website API + database
    Task->>SDK: enter with session, which calls start()
    SDK->>API: Claim this session
    API-->>SDK: Acknowledge
    loop Each trial in the selected run
        Task->>SDK: run.next_trial()
        SDK->>API: Read current progress
        API-->>SDK: Completed and unconfirmed trials
        SDK-->>Task: Next Trial, None if done, or error if unconfirmed
        Note over Task,SDK: Continue only when a Trial is returned
        Task->>SDK: run.media(trial)
        SDK-->>Task: Verified local path
        Task->>SDK: run.record(trial, "started")
        SDK->>API: Store started event
        API-->>SDK: Acknowledge
        Note over Task: Present with PGL / PsychoPy
        Task->>SDK: run.record(trial, "completed", result=...)
        SDK->>API: Store completed event and optional result
        API-->>SDK: Acknowledge
    end
    alt All session trials confirmed
        Note over Task: session.next_run() moves to later runs
        Task->>SDK: leave the with block, finish("completed")
        SDK->>API: Validate completion and release claim
        API-->>SDK: Acknowledge
    else Stopped early
        Task->>SDK: leave the with block, finish("interrupted")
        SDK->>API: Release claim, retain trial state
        API-->>SDK: Acknowledge
    end
    Note over Task,API: Reopen the same experiment, subject and session on another device
    Task->>SDK: session.next_run() then run.next_trial()
    SDK->>API: Read saved run and trial progress
    API-->>SDK: Server-authoritative state
    SDK-->>Task: Next Trial, None, or unresolved-trial error
```

Completed trials stay completed. An unconfirmed start anywhere in the session
blocks every run until reviewed; recovery policy is not yet implemented. Retry
unacknowledged operations unchanged on the **same Session object**. An active
presenter requires explicit `start(takeover=True)` to replace. An error inside
`with session:` finishes it as `failed`.

`session.next_run()` is the next unfinished run; `session.run("latest")` is the
most recently started one. `run.results` reads reported results from the server. Deleting local downloads never resets server progress.

[API objects](api-classes.md) · [Metric storage and filters](metrics-and-filters.md)
