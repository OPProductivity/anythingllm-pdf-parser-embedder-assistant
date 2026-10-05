# Archaic path review and contained corrections

## Scope and boundaries

Baseline: `af8e7cc`, following the October 4 runtime-sharing, v1.17 batching,
integration-cleanup and partial-rejection commits. The review mapped imports,
top-level definitions and production references across 63 tracked production
and support modules, then read changed paths and adjacent version, callback,
reconciliation, recovery, artifact, settings and segmentation contracts.
This is a repository-wide dependency audit with targeted deep investigation,
not a certification of every possible PDF or every line in the two monoliths.
The primary archived assistant history and relevant working-style history were
consulted; historical claims were checked against current source and fixtures.

The qualified v1.16.1 provider policy remains active only for that exact version.
v1.17 retains its native SDK policy. The helper-only worker facade and legacy
serial-recovery inputs remain intentional compatibility support. The guarded
Desktop refresh bridge is optional, not part of normal ingestion; its backup
provenance and renderer races belong to the separate bridge/API review.

## Corrected active defects

- Both reconciliation paths now use one pending-work predicate. Recent owned
  source-staging events can renew observation before the first namespace write;
  an active last document is not confused with a completed queue. The existing
  90-second quiet boundary is retained. Unknown/disconnected activity and
  terminal rejection/ambiguity events do not renew it. This never establishes
  vector success or retry permission and does not alter ETA/provider requests.
- Provider batch detail remains capped at 256 entries. Run-local numeric
  identity totals remain separate, so terminal replay and later sources cannot
  truncate totals or invent recovered events for already-observed batches.
  A complete 261-batch replay now retains 261 batches/chunks/ms in totals,
  256 detail entries and truthful complete coverage, even with repeated terminal
  events. The compact identity accumulator is in memory, not another artifact.
- Latest-run recovery resolves all root, per-document and queue-group manifests
  to their owning run beneath the private state directory. An escaped path
  cannot claim ownership.
- Restoring a duplicated environment assignment updates/removes every occurrence,
  matching the reader's last-assignment semantics and the existing write path.
  Unrelated settings remain untouched.
- Prepared-recovery diagnostics recognize staged queue rejection as a preserved
  rejection, not uncertain replay authority. Its historical state name remains
  readable. Count semantics clarify that staged document storage is not vector
  proof; no numeric field or historical receipt is rewritten.
- Unknown recovery policies fail closed before queue inspection/action.
  `automatic_recover` remains observation-only for both direct and background
  callers. The retired automatic cancel/restart/resubmit branch is removed;
  explicit operator cancel/restart controls and their call signatures remain.
- Paragraph-boundary scoring no longer discards its separator before testing
  for it. Sentence/abbreviation matching examines only a necessary trailing word.
  The last-complete-sentence matcher excludes punctuation-free tails from its
  backtracking search. Sentence-span and abbreviation decisions remain equivalent
  to the former matchers; restored paragraph preference is the intended change.
  Page-preserving records within the ceiling and whole-file mode are unchanged.

## Removed inactive helpers

The following had neither production nor test callers, after reference and AST
inspection: `outline_chapter_map`, `outline_chapter_for_page`,
`merge_short_page_segments`, and `split_page_under_limit_with_offsets`.
The active hierarchical outline and semantic segmentation implementations remain.
Other low-reference public/diagnostic helpers were not indiscriminately deleted.

## Verification evidence

- Initial focused settings/recovery/segmentation checks: 69 passed.
- Expanded focused correction and adjacent diagnostic checks: 127 passed.
- Adjacent pipeline reconciliation/source-atomic/outline checks: 32 passed.
- 2,000 generated sentence-span cases matched the old matcher exactly.
- 3,000 abbreviation/boundary cases matched the unoptimized scorer exactly,
  holding the intentional paragraph correction constant.
- Read-only archived-text replay covered 47 distinct PDF identities and 1,377
  unique single-page texts in three modes: 4,131 text-preservation/offset checks,
  no reader errors, completed in 42.124 seconds. Thirty unique whole-file/grouped
  records also matched the baseline's no-local-segmentation output exactly.
  This is not fresh OCR or a fresh embedding-speed benchmark. Original PDFs,
  retained run artifacts, outputs, workspaces and embeddings were not modified.
- A 20,400-character incomplete-fragment control returned the same absent span:
  baseline 6.2428 seconds, corrected 0.0011 seconds. The earlier broad comparison
  was stopped after over 300 CPU seconds; a bounded intermediate attempt reached
  its 180-second diagnostic budget. Neither was called successful qualification.
- The baseline full offline gate had 2,362 passes, one skip and two timing-sensitive
  failures. The success-only diagnostic fixture now has a separate startup budget;
  its timeout arm is still one second. The controlled HTTP tracker test joins the
  completed thread before asserting exit. No production timeout was relaxed.
- Ruff passed and Pyright reported zero errors/warnings. Dependency deprecation
  warnings and the optional-symlink skip are not hidden.
- Final full offline gate: 2,419 passed, one skipped, 34 deselected and 15 subtests
  passed in 653.44 seconds. Three dependency deprecation warnings remained;
  neither baseline timing failure recurred with the corrected test contracts.

## Deferred and separate work

No OCR-recognition, metadata, source-atomic backend, HTTP framing, retry, physical
provenance, native cache, API-key cleanup, database index/journal, UI-design or
output-retention change is included. The user-authorized parallel review owns
HTTP, cache, provenance, SDK-cause and key-cleanup corrections and live isolated
embedding qualification. Larger module decomposition, contract-wide state naming
and recovery-artifact consolidation remain deferred rather than rushed changes.

Deployment requires restarting only the idle PDF assistant. Its Desktop backend
bytes are unchanged; AnythingLLM Desktop does not require a restart.
