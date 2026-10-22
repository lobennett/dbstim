# dbstim API design

Status: approved 2026-10-20; implemented locally, pending server deployment.

## Purpose

Create experiments, obtain assigned stimuli, and report presentation progress through
dbstimuli.com. PGL/PsychoPy own presentation; the server owns assignments and progress.

## Objects

| Object | Proposed core methods |
| --- | --- |
| Client | metrics(), experiments(), experiment(name), create_experiment(...) |
| Experiment | summary(), publish(), subject(id) |
| Subject | session(id) |
| Session | download(directory), run(id="latest"), start(), finish(status) |
| Run | next_trial(), media(trial, mode), record(trial, status, result=None) |
| Trial | Immutable data; no methods |

Client authenticates with username/password; URL defaults to https://dbstimuli.com.
Keep conventional connection cleanup. Client and Session have four core methods
rather than combining unrelated operations. Loading by exact name defaults to the
authenticated owner and latest published revision, or a draft if unpublished.
Ambiguous names require an ID. Explicit revisions remain addressable.

Trial contains trial_id, stimulus_id, position, role, media_type, and source segment.
Run.media resolves its location without changing assignment data. Typed definitions
live in models.py; workflow objects and HTTP transport remain separate. Results and
stimulus measurements are separate from Trial.

## Assignments

Creation saves a draft with subjects, sessions_per_subject, runs_per_session,
trials_per_run, foils_per_run, repeats_per_run, filters, sampling method, and seed.
Sessions/runs default to one; counts are uniform. Publishing freezes assignments.

For T trials, F foils, R repeats: T−2F−R full videos, F initial halves, F complementary
halves, and R repeat presentations; T−F−R distinct parents per run. Reject infeasible
counts. Repeats replay an earlier initial presentation; initial halves precede foils.

shared_fraction applies to each subject's unique parents across the experiment.
Every subject gets the same shared sources, with independently seeded ordering.
Proposed defaults: round the shared count down and show achieved fraction; retain
shared sources' full/half roles; nonshared parents are disjoint across subjects/runs.
Reject insufficient eligible pools; never silently shrink assignments.

## Progress and media

Reopening a session loads existing records. “Latest” means most recently started
run, or the first planned run if none started. Completed runs stay completed;
explicit run IDs select later runs. next_trial() refreshes server state, returns
the next unstarted trial, or None when complete. Unresolved starts block resumption.

record() sends started/completed events with optional task-reported results.
Retries reuse event identities. The server acknowledges changes; failures stop
advancement. start() claims one active presenter; takeover requires explicit review.
finish() accepts completed/interrupted/failed; completion requires all trials confirmed.
No automatic replay, wrapper, or fabricated viewing confirmation.

Media modes: local verified files, on-demand cache, or temporary authorized remote
URLs. All resolve the same pinned rendition, including rendered halves. URL-capable
players can stream; others cache. Proposed default: download before presentation.

## Delivery

Rename repository/distribution/import to dbstim; update known consumers. Extend the
server before releasing the SDK; preserve existing manifests/events and map legacy
blocks to runs in one session. Keep old pinned releases usable.

Keep one notebooks/api.ipynb walkthrough with direct calls and uncommitted
credentials. Each Markdown document stays ≤500 words.
Test allocation, two-client resume/conflicts, retries, media identity, and legacy
compatibility. No production experiment or progress changes during verification.
