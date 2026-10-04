# Request-scoped provider batching for Desktop 1.17.0

Qualification: 2026-10-04. This extends the existing native upload contract,
not parsing, OCR, the visible interface, ETA, security policy or output cleanup.

## What history established

There are three different batching boundaries:

- The assistant still submits several PDF sources in one bounded, serial
  AnythingLLM API request (up to four sources / 512 records).
- Native OpenRouter `embedChunks` combines chunks within one document.
- The older source-atomic adapter combined chunks across the many page-parent
  records belonging to one PDF. This was guarded for exactly Desktop 1.16.1.

The last boundary became inactive on the 1.17 upgrade because its package,
backend identifiers and mutation authority had not been requalified. It was
not removed by the later security changes. Whole-file documents still retained
native within-document batching; one-chunk page-parent records did not.

The preserved assistant history and Git revisions show successful source
staging, rather than just accepted HTTP requests:

- `9ccd7fe`: live server staging activation, 2026-08-31.
- `add6ef0`: 413 fresh records from two sources, 13 provider batches and
  29.743 seconds of provider staging, with exact vector confirmation.
- The 2026-09-01 five-PDF run: 119 fresh records, six provider batches,
  approximately 31 seconds of staging and 1m34s total processing.
- The 2026-09-03 nine-PDF run: 1,718 fresh records, approximately 51 provider
  batches and 10m25s total processing.

The fastest observed batch-cap experiment used 28 inputs: a 300-record test
staged in 16.994 seconds, versus 46.817 and 58.292 seconds with a cap of 36.
Those were not randomized paired trials, so they do not establish a universally
optimal cap. The user subsequently requested restoring 36. This adapter retains
36 rather than silently changing that policy.

An early historical implementation paid for provider requests before discovering
native cache hits. Later server revisions fixed this with cache-first planning.
The October exploratory candidate that repeated this mistake was rejected.
This implementation reuses the final cache-first server body, not the older
worker body or that rejected experiment.

## Contained implementation

`anythingllm_source_atomic_v117.py` translates the assistant-owned staging body
to the exact, hash-qualified 1.17 backend identifiers. The original backend
function body remains unchanged as its fallback. Only the authenticated v1 API
handler receives a fourth argument, derived from a strict boolean
`pdfAssistantSourceAtomic` request field. Desktop's UI/native worker handler
is untouched; unmarked requests and other embedding engines retain their
original route.

The assistant sends this flag only for local requests containing multiple
records of a known PDF. Whole-file-only batches retain native chunk batching.
Unknown package fingerprints, contracts, backend bytes or changed backups
cannot install this adapter. Installation preserves a verified pristine backup
and requires a later Desktop root-process start before reporting activation.

Provider requests remain serial. The adapter adds no retries and honors the
native SDK's existing timeout and retry configuration, while retaining progress
heartbeats. It does not reinstate the older 35s/45s policy: live native controls
accepted valid 42s and 60s responses, so that policy could prematurely reject a
healthy source. Cache hits are resolved before planning provider inputs. Returned rows
are mapped by their explicit input indices; missing, duplicate or out-of-range
indices, non-finite vectors and inconsistent dimensions reject staging before
namespace mutation. Every fresh vector for a source is validated before its
cache writes begin. The existing exact-vector checks, source rejection,
ambiguous-commit handling and cancellation ownership remain in place.

## Live evidence

Each measured arm used empty isolated storage and fresh production-worker PDF
preparation, with no reused TXT exports or embedding vectors. Eight different
All sources PDFs produced 135 selected page-parent records. Both arm orders
were exercised. The installed research Desktop and its storage were not mutated.

| Upload/embedding phase | Native | Batched | Provider requests |
| --- | ---: | ---: | --- |
| Native then batched | 368.344s | 51.441s | 135 to 8 |
| Batched then native | 298.635s | 83.693s | 135 to 8 |

All 135 records were confirmed in both arms. Full stored text bodies matched;
metadata headers matched after excluding only the generated publication time.
Stored 4096-dimensional vectors matched the measured provider input/response
mapping (minimum cosine 1.0). Cached follow-ups confirmed all records in a new
workspace with zero provider calls.

Additional controls passed:

- Final integrated adapter, without harness-injected flags: 135 fresh records,
  eight provider calls, 87.320s upload/embedding; cached replay made zero calls.
- Native-SDK-policy repeat: 135 fresh records, eight provider calls and
  69.865s upload/embedding, with identical stored text and zero-call cached replay.
- Mixed cache: nine cached records plus 23 new records; only the 23 misses were
  sent to the provider, and all 32 records were confirmed in the new workspace.
- Whole-file control: two records / 12 vectors; text and mapping matched native
  ingestion. Native already used two calls, so whole-file-only requests are not
  opted into cross-record staging.
- Book boundary: 36 records in one provider request, with exact vector proof.
- Larger book excerpt: 51 fresh records in requests of 36 and 15; all vectors
  confirmed, followed by zero-call cached replay.
- Native SDK retry canary: an injected 503 with the existing SDK retry setting
  enabled produced one SDK retry, no additional adapter retry, and 32 confirmed
  records. A later canary added 40 seconds of artificial response delay; its
  valid first response took 75.777 seconds including that delay. All 32 records
  still confirmed without a premature liveness failure or duplicate submission.

The timings above are upload/embedding timings, not full PDF-run timings or a
promise for every provider response. The paired savings were 316.903s and
214.942s, substantially above the user's minimum useful optimization threshold.

All completed trial profiles, test workspaces, documents, vectors and caches were
disposed. Two aborted harness setup profiles remained because recursive removal
was denied; their test workspaces and credentials were explicitly removed, and
both contain zero document-vector rows. Original source PDFs were preserved.

Regression checks: 2,322 offline tests passed, one skipped, 34 deselected, plus
30 focused adapter/routing checks and 25 existing v1.16 compatibility checks
passed. Ruff and Pyright passed. Later live
activation must be reported separately from code integration and publication;
restarting the PDF assistant alone cannot activate a patched Desktop backend.
