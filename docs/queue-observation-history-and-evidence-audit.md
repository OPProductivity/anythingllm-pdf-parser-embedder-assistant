# Queue observation and evidence investigation, October 3, 2026

## Scope

This investigation follows the four-run audit of October 2. It preserves UI
design, ETA, OCR recognition/selection, segmentation, upload authority, duplicate
protection, private historical artifacts and public output retention. No source
PDF or AnythingLLM workspace/document/vector was modified in qualification.

## Misleading truncation: history and replacement

Commit `0010d89` (August 25) introduced a bounded 96-event ledger tail to prevent
ever-growing SSE arrays being repeatedly rewritten during large selections.
The ledger writer kept an honest count when given the full input array. The
source-window aggregator also carried a count. The separate shared queue-group
aggregator, however, clipped its input before final ledger serialization without
carrying the count. The writer could only see 96 items and reported no truncation.

The 19:54 and 21:21 runs both demonstrate this. This predates the October 2
compact JSON/shared snapshot changes. Restoring unlimited arrays would restore
the old disk/write amplification and would not repair missed network events.

The aggregation operation is replaced with `_merge_embedding_runtime_events`,
used by both aggregators. It preserves the full observed count independently of
the capped tail, including a child whose own tail was already capped. The ledger
then derives truncation from those two quantities. Tests cover multiple groups,
already-capped children, and a small queue which must not claim truncation.
Historical logs are not rewritten or supplemented with invented events.

## Missed SSE events

The old urllib observer applied a five-second socket timeout to a heartbeat-free
stream. After an otherwise healthy quiet interval, it disconnected and waited
0.75 seconds before reconnecting. The Desktop feed has no replay mechanism.
Events delivered during that gap were unobservable. This explains a concrete
loss mechanism and is consistent with missing completion/start pairs around a
slow record; it does not retrospectively prove the cause of every missing event.

A local HTTP regression reproduced event loss and slow observer termination.
A synchronous socket-interruption prototype failed the Windows termination test
and was removed. The user approved replacing only the advisory SSE transport
with an async HTTPX stream. HTTPX 0.28.1 was already installed through Gradio and
is now an explicit pinned dependency; it was not upgraded.

Connection establishment remains bounded to five seconds. Established reads
have no idle deadline. `StreamStopEvent` schedules cancellation on the observer's
event loop, closing the HTTP stream promptly without waiting for incoming bytes.
Actual EOF/transport failure still reconnects with bounded backoff and now leaves
diagnostic evidence. The route's 404 fallback, path correlation, deduplication,
and prohibition on authenticated redirects remain. Upload transport is unchanged.

Tests exercise a real loopback server, a quiet interval over five seconds,
mid-queue completions, fast stop during a blocked read, repeated real EOF,
redirect rejection, stop-before-connect and no late callbacks after ownership ends.

## Waiting: deliberately small change

The immediate stop also removes the former observer join's potential one-second
tail after exact vector confirmation. The blocked-read test requires the observer
to terminate within 0.3 seconds. This is not a promise that every real run becomes
one second faster: the old thread could also happen to be between reads.

The existing cheap SQLite commit hint remains responsible for triggering exact
physical verification when final SSE events are missing. It still cannot prove
success, authorize retries or reset liveness. Two-second storage polling, finite
stall/deadline rules and ETA remain unchanged. The new stream reduces quiet-time
event loss rather than masking it with longer timeouts or relaxed confirmation.

## Native-body OCR: deeper findings

The four functions controlling this route are AST-identical in `d32145b`,
`d9aed10`, `92f7f73`, `b83957b` and the current extraction code:

- `_reocr_confirmed_native_body_region`
- `_native_body_reocr_decision`
- `native_layout_ocr_page_evidence`
- `apply_region_aware_native_layout`

Fresh extraction of all seven Garncarz pages reproduced the retained selected
text on pages 1 and 7 after the same text sanitation and normalization. Comparing
raw OCR directly to sanitized manifests initially differed, as expected; that
is not evidence of a recognition regression. The two run-level TXT hashes also
differ because the two segmentation modes assemble the document differently.

Complete rendered source pages 1 and 7 were inspected. Page 1 contains a partial
adjacent page and margin noise; body recovery deliberately crops those away.
Page 7 is a straightforward single-column notes/reference list. Its OCR output
splits reference 24, moves its title continuation near the end, and similarly
detaches `Film History`, `New German Critique, 29` and part of reference 34.
The visible source does not have that ordering. Ordinary OCR spelling errors
also remain, such as `Gamnearz` instead of `Garncarz` in one reference.

The cause is broader than sparse logging: native-body OCR uses Tesseract PSM 4
for this page, and its acceptance gate checks content length, alphabetic coverage
and noise-score improvement. Those checks can pass while citations are reordered
and names are misspelled. Here coverage is 1.0004 and the measured noise score
falls from one to zero. Neither metric establishes correct reading order.

The simplified OCR page ledger records method and disposition but not detailed
recognition-call measurements. The layout review contains more evidence: crop
body bounds, selection reason, PSM choice, noise scores and coverage. Detailed
render/crop mechanics can be recovered from the unchanged code (2x rendering,
1% horizontal guard, 6.5%-95% vertical crop), but that is code-derived rather
than a retained per-call receipt. The adapter does not implement the separate
region OCR quality/drop-cap/crop-retry assessments; `not_assessed` is truthful.

No OCR algorithm or source-specific rule was changed in this investigation.
Changing recognition or its selection gate requires its own measured comparison
against native text and source images, including preservation of the successful
margin recovery. A blanket rollback of logging would not fix these defects.

## Why identical files remain

The copies originated in the existing candidate-selection and metadata testing
contracts, not the new snapshot module:

- Candidate bodies/manifests preserve backend-comparison evidence. The selected
  root material is the upload/recovery contract. A post-selection author recovery
  can make the selected manifest differ from its candidate predecessor.
- `anythingllm-upload.txt` is the established internal body path; the descriptive
  parsed TXT is an export-facing name. Existing readers target those paths.
- Strict/native-header and segment/page-parent variants support different metadata
  and upload plans. Their bodies can be identical for a particular source while
  plan metadata and record boundaries differ elsewhere.
- Manual test kits and small compatibility-probe kits are standalone packages
  with their own upload plans/checklists. Their production materialization
  was already present in portable packaging commit `dc39078`; the code keeps
  them while preparing file-upload verification material.

These are valid reasons to maintain distinct artifact roles. They do not prove
each role needs a physically separate copy of identical bytes. About 1.3 MB per
new run remains a future consolidation opportunity, but removal/linking must
account for mutations, independent cleanup and standalone bundle portability.
No copies or original artifacts were removed here.

## Are hashes appropriate?

Hashes are not intrinsically the best representation for every record. Here a
SHA-256 name gives an immutable snapshot a content identity and detects corruption;
identical canonical JSON values share one file, and concurrent writers cannot
silently overwrite different contents. Semantic filenames alone would not provide
those properties, though they would be easier to browse.

The measured alternative is compact, self-contained JSON without shared snapshots:
3,839,328 / 3,963,442 bytes for these runs' expanded non-pool JSON. Referenced JSON
plus pools is 1,336,209 / 1,253,632 bytes, approximately 65% / 68% smaller. This
comparison holds the run data fixed and does not attribute different PDF sizes
to the serializer. Earlier smaller successful folders often achieved size by
pruning diagnostics, which conflicts with preserving private investigation logs.

The tradeoff is real: raw summaries are no longer individually self-contained.
`read_run_json` and `python -m run_evidence` restore their ordinary JSON view, and
a complete copied run remains portable. Bare hashes cannot recover missing data.
Superseded preparation snapshots also need a clearer role/history index; content
hashing is not a substitute for that index. This format was not broadly reversed.

One new regression was found and fixed: the opt-in diagnostics exporter selected
summaries but omitted their snapshots. It now collects only verified transitive
dependencies, preserves the common run parent in its existing ZIP layout and
blocks incomplete/corrupt bundles. Four real retained source summaries were
exported to temporary bundles, moved/extracted and expanded identically. Unrelated
snapshots are excluded. Normal TXT exports and output-folder treatment do not change.

## Qualification Boundaries

The production-path regression suite passed 965 tests plus 15 subtests. Final
stream/count checks passed nine tests, and diagnostics/snapshot checks passed 36.
The final adjacent stream, snapshot and private-retention batch passed 40 tests.
Core type checks, the observer module's type checks, Ruff and diff checks passed.
The counts overlap and should not be added as unique test totals.

The local HTTP tests qualify the actual observer/parser/cancellation path on
Windows, not a mock claim that events exist. They cannot guarantee Desktop emits
every expected frame or that a future genuine disconnect loses no events, since
the server provides no replay. Exact physical vector proof remains mandatory.
No new live embedding or semantic retrieval test was performed.
