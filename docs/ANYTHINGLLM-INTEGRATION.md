# AnythingLLM Desktop integration

This document describes the boundary between the assistant and AnythingLLM
Desktop. It is intentionally precise about what the assistant can observe and
what it must not assume.

## Ownership and API boundary

The assistant talks to a running local AnythingLLM Desktop backend through its
local workspace/document APIs. It prepares text records before submission so
their filenames, metadata, and provenance can express the originating PDF and
page or page range. Provider credentials remain configured in AnythingLLM
Desktop; this project does not read, store, or commit a provider key.

AnythingLLM owns its global embedding queue, provider calls, vector storage,
and final internal splitting. The assistant therefore cannot equate an HTTP
acceptance response with embedding completion, and it does not promise that a
single prepared record will remain a single internal chunk. Its page-aware
contract is instead based on the identities and provenance of the records it
submits and later confirms.

## Page-parent identity contract

For ordinary page-preserving operation, the assistant produces records tied to
the actual source page or source page range. Each expected current-run record
has a page-parent identity and matching provenance. The normal completion
proof is that the expected identities have corresponding searchable-vector
evidence in the target workspace.

This is stronger than any one of the following observations:

- an upload HTTP request was accepted;
- a file appears in the AnythingLLM Documents drawer;
- a queue progress message advanced; or
- a broad workspace storage scan found something similar.

Those observations can support diagnosis, but they are not interchangeable
with exact current-run identity proof. A broad storage audit remains a
post-completion diagnostic so ordinary successful runs are not blocked by
unrelated historical workspace content.

## Completion evidence

The assistant reports separate stages rather than collapsing them into an
unqualified “uploaded” result.

| Evidence | Meaning | Ordinary success role |
| --- | --- | --- |
| **Prepared locally** | Transcript and selected segment records were written. | Necessary, but not an AnythingLLM result. |
| **Stored** | Expected records reached the selected workspace/document path. | Useful evidence; not alone sufficient. |
| **Vector confirmed** | Exact expected page-parent identities have matching vector/provenance evidence. | Normal page-aware completion boundary. |
| **Runtime retrieval verified** | A live vector/search check returned matching content. | Optional diagnostic after exact proof. |

When exact vectors are confirmed, an optional live retrieval timeout is
recorded as a non-blocking diagnostic. It must not turn an otherwise proven
workspace into a failure or make a user wait for a speculative search probe.

## Queue and observer behavior

The managed upload route submits one app-owned Desktop FIFO request for the
planned current-run locations. The assistant observes only queue events that
can be associated with those locations. Desktop's queue and provider activity
are global, so the assistant deliberately avoids client-side queue concurrency
and automatic retry storms: either could collide with unrelated Desktop work
and make ownership ambiguous.

Queue observation, storage reconciliation, and vector confirmation are
concurrent evidence streams. Their percentages must not be added as serial
work. The visible progress bar uses a run forecast; the stage text carries the
specific current observation. An ETA may be revised after material queue or
vector evidence, but it is not proof that the forecast is accurate.

The current Desktop route still processes page-parent records serially inside
the accepted workspace queue. A single outer request does not imply one
provider embedding request. The safe performance direction is an upstream
Desktop batching change that batches provider inputs while retaining one
ordered page-parent identity and progress record for each vector. That source-
level Desktop work has not been substituted with unsafe client concurrency or
automatic retries in this project.

### Expected transient states

The following can be legitimate diagnostic states and must not be reported as
failures merely because they occur:

- SSE observer connecting, reconnecting, or temporarily quiet;
- no recent owned queue event while exact vector reconciliation continues;
- a bounded Desktop receipt timeout followed by exact vector proof; and
- an optional final retrieval check that is deferred after exact proof.

A result becomes actionable when exact expected vectors are missing after the
owned observation/reconciliation window, identity/provenance does not match,
or the run is cancelled/ambiguous. The generated run report is the source for
that diagnosis.

## Recovery and ownership safety

If a request outcome is ambiguous, the assistant uses one bounded observation
window shared by queue, storage, and vector checks. A later observer consumes
the remaining time instead of starting another independent full deadline.

Automatic recovery is intentionally narrow. It may act only when work is
ledger-proven as belonging to the current run. Manual activity, ambiguous
ownership, and unrelated queue state are preserved rather than cleared or
restarted. This protects an operator's existing AnythingLLM work from an
assistant that cannot prove ownership.

## Batch picker and workspace safety

Batch-folder discovery is a local selection step, not AnythingLLM evidence.
Its scan state is cleared on browser reload or localhost restart so an old
folder result cannot silently become a later submission. The number shown
during a scan counts local files and folders inspected, not the number of PDFs
that will be uploaded.

The assistant does not overwrite or delete existing AnythingLLM workspaces.
For upload runs, choose an existing workspace or explicitly request a new
workspace for the document. A new workspace is created only after run
confirmation.

## Optional Desktop refresh bridge

The refresh bridge is opt-in. It patches an installed AnythingLLM Desktop
archive only when required anchors are present, creates a timestamped backup,
and provides validation and uninstall commands. Close AnythingLLM Desktop
before changing the bridge and retain the backup until normal behavior is
confirmed.

```powershell
anythingllm-pdf-assistant bridge validate
anythingllm-pdf-assistant bridge install
anythingllm-pdf-assistant bridge upgrade
anythingllm-pdf-assistant bridge uninstall
```

## Troubleshooting and privacy

- Run `anythingllm-pdf-assistant doctor` before reporting a startup problem.
- Verify the configured embedding provider independently before diagnosing the
  assistant's progress model or queue observer.
- Read the run summary for the stage that actually failed; do not infer failure
  from an ETA, one temporary observer state, or one drawer row.
- Never publish provider keys, private PDF text, absolute source paths,
  AnythingLLM Desktop storage, workspace data, or raw local run timelines.
