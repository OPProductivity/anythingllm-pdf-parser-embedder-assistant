# Current engineering state

Status: 2026-07-27. This is the public engineering contract for the current
repository. It deliberately excludes private PDFs, local paths, credentials,
workspaces, source fingerprints, raw timing traces, and operator handoffs.

## Product boundary

The assistant is a local PDF-preparation and orchestration layer for
AnythingLLM Desktop. It can create local text artifacts or prepare page-aware
records, submit them to a local workspace, and independently observe
submission, owned queue activity, exact persisted vectors, and optional runtime
retrieval.

It does not replace AnythingLLM or retrofit reliable page provenance onto a
whole-document vector. AnythingLLM can accept PDFs itself; this project
prepares page-aware text records when a controllable, inspectable provenance
boundary is required.

## Decisions to preserve

- **Page provenance is substantive.** `Page – preserve automatically` creates
  one page-parent record per ordinary source page. Oversized pages may have
  page-local children, but a child never crosses a page boundary.
- **One owned Desktop FIFO request per run.** Do not reintroduce one
  application-level `update-embeddings` request per page.
- **Evidence layers are separate.** HTTP acceptance, owned Desktop SSE events,
  exact persisted identities, and runtime retrieval prove different things.
- **Exact vector proof is ordinary success.** A live retrieval timeout is
  diagnostic and non-blocking once the expected identities are exactly proven.
- **OCR is conditional.** Native extraction is the fast path; OCR or
  layout-aware processing follows page-level readiness evidence. OCR review is
  not upload success.
- **No observer thread mutates Gradio components.** Observers collect evidence;
  the foreground handler owns visible UI updates.

## Queue, recovery, and progress

Desktop queue and vector materialisation overlap. Queue and vector `x/y` are
concurrent evidence, so the visible bar must use their furthest proven fraction,
never their sum. A healthy, owned SSE queue permits sparse local storage checks;
uncertainty, quietness, reconnecting, or a mismatch restores exact checks.

Reconciliation has one bounded deadline. Cancellation freezes the last proven
checkpoint. Recovery and any automatic resume are restricted to durable,
ledger-proven locations owned by the current run; manual or ambiguous activity
blocks mutation. Broad storage audits remain post-completion diagnostics, not
ordinary success blockers.

The display uses a current elapsed-plus-remaining forecast alongside owned
evidence. It must remain monotonic, explain ETA reprices, and stop advancing
when owned activity becomes stale. Current top-level display allocation is:

| Display interval | Meaning |
| --- | --- |
| 0–0.5% | Run readiness and local setup. |
| 0.5–97% | PDF preparation plus overlapping queue/vector evidence. |
| 97–100% | Durable validation, report, and download handoff. |

## Benchmark and privacy contract

The formal benchmark invokes the real Automatic handler. One excluded warm-up
precedes two serial ordinary trials for each approved anonymous document.
Public artifacts contain only anonymised IDs, page count, size, OCR risk,
safe configuration categories, and aggregates. Real source paths, filenames,
fingerprints, workspace names, credentials, and raw event timelines stay in
ignored local evidence.

Benchmark reports expose two views. **Operational** metrics include all
completed runs, including observer uncertainty and warnings. **Calibration**
metrics include only successful, environment-comparable, observer-healthy runs
that match the current presentation-controller and runtime-protocol revisions.
Only calibration metrics may influence the progress model. A warning is not
ordinary calibration evidence unless a named, tested timing-neutral exception
is added first.

Each run records a privacy-minimal timing timeline beside its private artifacts.
The local timing model may retain source identity for repeatability, but that
identity must never be committed.

## Maintenance

Before changing code: inspect `git status`, add focused tests, compile touched
modules, restart the managed localhost app when runtime code changes, and check
`http://127.0.0.1:7860/`. Historical experiments and stale benchmark cohorts
are not calibration evidence. `commit git` is local only; publishing requires
explicit user instruction.
