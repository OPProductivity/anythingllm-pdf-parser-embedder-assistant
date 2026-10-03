# Submission vector confirmation qualification

## Cause and historical boundary

Run `r-20261003-140829-d4c0bd21ea` prepared three whole-file records for
`thesis3`. The confirmation callback raised `UnboundLocalError` while building
an identity-only status. Confirmation lasted about 2.07 seconds, not an
exhausted reconciliation window. Desktop continued indexing independently.

Commit `0e9990b` (August 31) removed the fallback message but retained a
publication predicate admitting positive identity-only observations. The
pre-commit branch handles that condition. Restoring workspace-wide counts as
submission ownership would instead reintroduce historical duplicate errors.

## Contained implementation

- A total status formatter handles mapped-record, identity-only, and quiet
  recovery observations. Existing zero/unchanged status suppression remains.
- Record progress uses distinct mapped submitted documents when that field is
  available. Historical receipts lacking it retain their existing fallback.
  Owned internal-vector movement remains a separate liveness signal, so a
  record-count cap cannot conceal later internal-vector movement.
- Complete mapping candidates must pass a filtered vector-ID query in only
  the selected LanceDB namespace. Queries project only IDs, in batches of 256;
  text, embeddings, other namespaces, and whole-table materialization are not
  read. Missing or busy physical storage remains uncertainty, not permission
  to resubmit. An initial partial fast snapshot does not add physical reads.
  Explicit recovery/deep observations can prove partial exact locations.
- A source-local partial result uses physical location evidence when the
  verifier provides it; historical identity-only receipts keep their fallback.
- Callback exceptions retain classification, exception type and traceback.
  Unconfirmed affected PDFs receive a verifier-interrupted warning distinct
  from a timeout or a rejected upload. Independently confirmed siblings keep
  their success. Successful later outcomes clear the exception marker.

No OCR, segmentation, UI layout, ETA formula, cache identity, duplicate
preflight, SSE transport, upload replay authority, artifact layout, historical
run files, or public-output cleanup policy was changed.

## Qualification

Sixteen new deterministic cases cover the real production batch callback's
identity-only snapshot followed by expanded mappings, record/vector units,
physical absence, wrong namespace, old vector IDs, missing internal vectors,
busy storage, callback exception retention, source-local propagation, and
terminal classification. Physical-query fixtures use disposable local LanceDB
tables, not the user's AnythingLLM store.

The wider pipeline/observation/liveness/ETA/stream suite passed 983 tests and
15 subtests. After the source-local safeguard and two additional tests, the
focused batch passed 43 tests. Two additional cases confirm that partial fast
snapshots defer physical reads while explicit recovery checks partial exact
locations. The adjacent reset/recovery/artifact suite passed 115 tests. Ruff
and configured core Pyright passed.

Read-only production qualification reconstructed the last run's expected
payloads from its canonical upload plans. The updated verifier returned
`current_submission_provider_rechunked`: three covered records, nine mapped
vectors, all nine exact IDs present in `thesis3`. No upload, embedding,
workspace mutation, or historical run-record rewrite was performed.

Activation requires restarting the existing assistant server. The database
qualification imports the updated code; it is not evidence that the existing
server process has reloaded it.
