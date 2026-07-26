# Current engineering state

Status: 2026-07-26. This is the public, current engineering handoff for this
repository. It supersedes informal and older handoffs as guidance for present
behavior. Historical experiments are not promises about a current AnythingLLM
Desktop release or embedding provider.

## Product boundary

The assistant is a local PDF-preparation and orchestration layer for
AnythingLLM Desktop. It can create local text artifacts or prepare page-aware
text records, submit them to a local workspace, and separately observe
submission, queue activity, searchable vectors, and optional runtime retrieval.

It does not replace AnythingLLM or turn a whole-document vector into reliable
page vectors after the fact. AnythingLLM can accept PDFs itself; this project
prepares text records when a controllable, inspectable page-provenance boundary
is needed.

## Decisions to preserve

- **Page provenance is substantive.** `Page – preserve automatically` creates
  one page-parent record per ordinary source page. Oversized pages may have
  page-local children, but a child must not cross a page boundary.
- **One owned Desktop FIFO request per run.** Do not reintroduce one
  application-level `update-embeddings` request per page.
- **Evidence layers are separate.** HTTP acceptance, Desktop queue events,
  exact persisted identities, and runtime retrieval prove different things.
- **Ordinary success is exact and fast.** Exact expected page-parent identity
  evidence is the normal storage proof. A runtime-retrieval timeout must not
  block use of an otherwise verified workspace.
- **OCR is conditional.** Native extraction is the fast path; OCR or
  layout-aware processing follows page-level readiness evidence. OCR review is
  not an upload success.
- **No observer thread mutates Gradio components.** Observers collect
  evidence; the foreground run handler owns visible UI updates.

## Queue, recovery, and progress

Desktop queue and vector materialisation overlap. The observer correlates SSE
events with locations owned by the active run, records observer health, and
does not treat unrelated manual work as this run's completion. Reconciliation
uses one bounded shared deadline with read-only checks; it must not begin a
second hidden waiting period after that deadline expires.

Cancellation is an operator outcome, not a generic failure. Local work stops
at a safe checkpoint and durable evidence remains. Queue cleanup or a Desktop
restart must be limited to ledger-proven app-owned locations; uncertain
ownership or manual activity means preserve evidence and avoid altering
AnythingLLM.

| Display interval | Evidence represented |
| --- | --- |
| 0–0.5% | Run setup and readiness preflight (`AUTOMATIC_RUN_PREFLIGHT_DISPLAY_END`). |
| 0.5–about 16% | Local metadata, extraction, candidate/payload/attachment preparation, then submission receipt. |
| about 16–78% | Overlapping Desktop queue and exact page-parent-vector observation; use the furthest proven `x/y`, never their sum. |
| about 78–98% | Retrieval, storage validation, and worker-side reporting. |
| 97–100% | Durable run report, downloads, and terminal handoff (`AUTOMATIC_RUN_TERMINAL_DISPLAY_START`). |

The decimal boundaries are the current provisional evidence allocation.
`AUTOMATIC_UPLOAD_PHASE_RANGES` in `auto_anythingllm_pipeline.py` is the source
of truth; the rounded descriptions above are explanatory rather than a second
set of ranges. Do not treat timings from an earlier presentation-controller
version as calibration evidence: a complete current-version cohort is required
before changing these ranges.

During ingestion, display progress is constrained by the same elapsed/remaining
forecast shown in the UI. The stage line's `x/y` values are direct evidence;
the ETA is still a forecast.

The visible numeric bar is monotonic for one run: a new queue, vector, or ETA
observation can improve the forecast but cannot move a shown checkpoint
backwards. Queue-rate ETA repricing waits for mature owned evidence, occurs at
most once per 30 seconds, and moves by no more than 25% per reprice. Early SSE
events remain useful stage evidence but are not a reliable countdown rate.

Each run writes a privacy-minimal `timing-evidence-timeline.jsonl` beside its
artifacts. It contains counters, durations, rate, and observer state—never PDF
text, keys, or absolute source paths. The central timing model may record local
document filenames, page counts, and phase timings for future benchmark
analysis, but filename-bearing rows, source paths, fingerprints, workspace
names, and raw timelines are private ignored application data. They are never
approved for Git; committed benchmark material uses random document IDs and
safe aggregates only.

The benchmark invokes the real Automatic handler and privately samples the
same one-second paced-progress value rendered by the localhost status panel.
That sample is calibration evidence only; raw UI timelines remain ignored local
data. A controller-version change makes prior benchmark trials stale until all
16 serial trials have been rerun.

## Current limits and maintenance

Large page-preserving PDFs can remain slow because Desktop processes many
document jobs and the embedding provider controls much of the latency. Exact
identity-set observation remains the normal check; broad storage diagnostics
are deferred to mismatch, recovery, an explicit audit, or a warning path.

Before changing code: check `git status`, add focused tests, compile touched
modules, restart the managed localhost app, and verify `http://127.0.0.1:7860/`.
Public fixtures and documentation must not include private PDFs, keys,
account-specific paths, storage databases, or paid-run receipts. `commit git`
is local only; `commit git push main` publishes only when explicitly requested.
