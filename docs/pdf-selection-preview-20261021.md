# PDF selection preview: implementation and qualification scope

The first ordinary multi-file selection previously deferred metadata while the
folder route inspected PDFs and ran a metadata preview. Retry followed yet
another chain. The resulting UI could be ready without metadata, or unavailable
without an explanation of the operation doing the work.

Direct, folder, retry and subset selection now share a streaming metadata
preview. It announces recognition before inference and publishes individual
results without a manual refresh. The supporting preview retains its five-PDF
limit, explicitly distinguishes that from the selected total, and never replaces
fresh authoritative processing of every selected PDF after Confirm.

Metadata appears before the ETA calculation. The estimate and optional workspace
identity work have separate announced stages. Actual live observation found an
estimate callback lasting 108.68 seconds while settings reset took 0.04 seconds;
those are observed UI callback times under the test workload, not recognition
benchmark results or universal performance claims. No OCR acquisition is removed.

Per-browser revision tokens suppress obsolete metadata, workspace and estimate
results after a new selection supersedes the old one. Tokens contain no source
or inference evidence. Inferred fields reset when sources change; explicit user
edits survive option changes. Untouched preview author/short-label values are
not promoted to authoritative override arguments at Confirm, so later role
evidence remains eligible during fresh preparation.

The folder list exposes validated source names before all-page native inspection
finishes. Programmatic component updates do not trigger duplicate option chains.
Confirm remains mounted while unavailable, and its immediate feedback supports
both native-button and wrapped-button DOM. Exact active labels avoid treating
the idle text 'Confirm and start processing' as an active run.

## Mixed selections and database contention

An actual browser session reproduced 22 chips after selecting an eleven-PDF
folder and uploading those same eleven files. The browser copies had different
physical paths. Clearing the ordinary picker after merging then dispatched a
second change callback, which returned an empty folder selection. Chips remained
visible while metadata cleared and Confirm stayed disabled.

Same-name, same-size, SHA256-identical uploads now map to existing folder paths.
Differently named aliases, different bytes and existing separate folder entries
remain. An empty picker acknowledgement preserves the selected subset. User
upload/delete/clear events own the selection chain; programmatic File updates
do not create another selection transaction. Identity digests exist only within
the merge call and contain no recognition evidence.

The final eleven-file mixed recheck found a second readiness defect: canonical
copy mapping changed the physical-path signature after the selection began.
The finish guard rejected that legitimate transition and left Confirm locked
after metadata had finished. A direct upload into an existing folder batch now
allows the final signature of that owned merge transaction. The browser-session
preview token still rejects obsolete transactions, and the acknowledgement is
consumed at finish. Direct selections without a folder keep the ordinary exact
signature guard. The regression test fails against the previous implementation
and passes with this acknowledgement; a separate test covers a newer selection
superseding the merge.

A later actual folder run stopped before workspace creation because the live
SQLite schema read returned SQLITE_BUSY. Its installed package fingerprint was
unchanged. A busy read now reports storage contention without claiming missing
columns or package incompatibility. Mutation capabilities remain blocked until
the schema can actually be read. No database lock, contract check or activity
condition is bypassed. Test discovery now isolates the default Desktop storage
under the temporary test profile as well as isolating the app's own data root.

## Validation and remaining live scope

- Development suite with the recognition candidate: 3,323 passed, one skipped,
  35 deselected, 28 subtests passed. The UI-only production integration separately
  passed 3,275 tests, one skipped, 35 deselected and 28 subtests before the
  recognition integration. After recognition integration and the two final
  canonical-copy readiness controls, the production source passed all 3,325
  tests, with the same skip/deselection/subtest counts.
  Coverage includes the actual locked/readable SQLite fixture, guarded mutation
  capabilities, exact browser copies, renamed aliases, equal-size different
  bytes, preserved existing entries and the actual selection callback graph.
- Before the storage diagnostic additions, the 24 focused picker controls and
  the 3,318-test full suite passed. Ruff and whitespace checks passed.
- Opt-in actual Edge client regression: native and wrapped button controls
  pass; the previous client fails the idle/active state control.
- Normal servers on both ports passed real browser upload of two small PDFs,
  user row removal to one PDF, and user Clear to an empty selection. Metadata
  updated without Refresh and Confirm followed the selection. This is actual
  File-event coverage, separate from the minimal client-only button fixture.
- Actual Gradio 6.19 direct and folder previews with the same eleven real PDFs
  reach ready and publish metadata without Refresh. The native OS folder-dialog
  return alone was supplied by a private harness. These checks do not establish
  default full processing or embedding success.
- An actual eleven-PDF direct run on the separate frozen installation completed:
  2,481 selected records attached and vector-confirmed, comprising 2,477 reused
  embeddings and four freshly embedded records. Targeted OCR contributed to six
  selected outputs (25 pages). Seven nonempty author names agree with the manual
  references; four sources abstained. The frozen recognition modules differ from
  current main. No live retrieval check was performed.
- The final actual eleven-file folder-then-direct-upload check reaches ready,
  retains exactly eleven canonical source chips and needs no Refresh. Its
  displayed metadata equals the clean direct-picker preview; all eleven input
  SHA256 records match. This is preview coverage, not full ingestion coverage.
  Full same-source processing on both routes and both ports remains incomplete
  because of the persistent live database lock.

The one successful run establishes selected-record/vector integrity, not complete
text equivalence or universal authorship correctness. The test observer records
primary inference separately from repeated supporting core calls and nested
preview helpers. It is not an author/OCR cache. The frozen installation retains
its original recognition modules; a UI overlay is not an upgrade of that source
snapshot. Recognition changes have their own accuracy and runtime gate.
