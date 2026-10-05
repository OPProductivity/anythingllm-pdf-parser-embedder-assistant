# Parallel correctness qualification

Four independent GPT-6 Astra agents on priority service qualified metadata,
interruption recovery, retained-history discovery and installed packaging.
The coordinator continued the segmentation/live matrix concurrently, reviewed
source logic and candidate diffs, and alone integrated the following changes.
A separate Astra agent handles same-browser clear/retry tests. No agent wrote
production source or restarted production services.

## P05: First-document cancellation history

The no-summary cancellation branch omitted the existing early-terminal history
helper. Actual background stream/browser-cancel A/B showed cancelled progress
but no terminal record/history on the baseline, and exactly one cancelled record
with zero accepted/vector counts on the candidate. Cancellation after a completed
earlier source and ordinary two-source success still finalize exactly once.
Only the existing helper call was added; UI tuple shapes, ownership/cancellation
authority, output cleanup and recovery submission policy are unchanged.

Twelve real-PDF isolated trials retained extraction exit86, result-publication
exit87 and checkpoint exit88 failures. Completed artifacts remained byte-identical
and incomplete evidence could not authorize replay. One initial publication
injection did not fire and is explicitly excluded rather than claimed successful.
The reusable first-worker-cancel regression fails on the baseline (zero history
calls) and passes after the fix; 50 focused history/recovery tests passed.
Evidence: `C:/Users/Ninkear/.codex/tmp/shared-p05-interruption-20261005/P05-report.md`.
No lab fault hooks were copied into production.

## P06: Damaged retained manifest discovery

A newer invalid-UTF8 recovery manifest raised UnicodeDecodeError and hid readable
older/nested evidence. Discovery already skips OSError/malformed JSON; only the
missing exception type was added. Malformed bytes remain untouched, and separate
most-recent ownership guards still refuse uncertain recovery authority.

Baseline malformed cases: two failures/five passes; candidate/adjacent tests:
100 passes, plus seven final byte-preservation checks. Synthetic 100/1000/3000
retained roots resolved workspace/nested ownership without changing fixture bytes.
Complete copied runs resolved canonical snapshot references. No retention,
pruning, ordering, ETA or discovery-speed change was made. Equal-mtime behavior
was stable on this filesystem, not claimed universally deterministic.
Evidence: `C:/Users/Ninkear/.codex/tmp/shared-p06-history-20261005/REPORT.md`.

## P07: Target-installed annotated OCR resource

Ordinary venv installation already worked. The supported pip --target layout
contained the correct annotated eng model, but duplicated model-path resolution
omitted target/share and silently selected the generic fallback. Model discovery
now uses the established package_resource_path helper. Size/hash checks, OCR
pixels/parameters and missing-model fallback are unchanged. Checkout model
arguments were compared before/after and are identical.

Wheel installations outside the checkout exercised actual child-worker PDF
preparation, adapter imports/assets, diagnostics, timeout cleanup and absent-run
recovery. Real Tesseract crop subprocesses verified model/PSM route selection:
ordinary venv and corrected target use existing tessdata_best_eng/psm6; baseline
target used installed_eng/psm4. Thirty-eight focused regressions passed. Natural
annotated-PDF transcription quality was not measured; no new recognition
algorithm or production OCR tuning is claimed.
Evidence: `C:/Users/Ninkear/.codex/tmp/shared-p07-package-20261005/REPORT.md`.
The integrated regression uses the real hash-verified packaged model in a fake
target layout, not a stubbed integrity check.

## Deferred Metadata Policy

P03 confirmed Mullins false abstention: filename-derived title contains the
visible affiliated authors, so title-fragment rejection suppresses them.
Physical cover recovery remains intact. Edited-volume and whole-journal identity
cannot safely be changed by assuming every filename contributor is the author.
No recognition policy was changed; a scoped prototype question was sent to the
user. Evidence: `C:/Users/Ninkear/.codex/tmp/shared-p03-metadata-20261005/REPORT.md`.

## Coordinator Checks

Integrated focused gate: 101 tests passed. Ruff and configured core Pyright
checks passed. Explicitly adding the legacy UI module to wider Pyright analysis
shows 150 diagnostics both before and after, with identical diagnostic content
and no newly introduced diagnostics; the existing configured gate excludes that
module. These pre-existing static issues were not silently refactored away.
Full offline gate: 2457 passed, one skipped, 34 deselected, 25 subtests passed
in 254.19 seconds; three existing dependency deprecation warnings. Activation
is coordinated separately after commit; AnythingLLM Desktop remains running.

## P04: Actual Same-Browser Reset Checks

The independent browser agent completed six three-PDF local diagnostic runs
without refreshing the page. All 18 serialized worker requests matched the
entered settings and original source hashes. Checks included X-clear-Y, retry,
saved future defaults, three different PDFs after Clear, and retry followed by
replacement with another disjoint three-PDF selection. No stale settings or
stale input identities were demonstrated, so no reset code was changed.

A separate read-only API fixture verified display-name refresh retained the
same confirmed workspace slug; removing that slug cleared its selection.
Actual Confirm acknowledgement/dispatch retained the renamed workspace identity.
The fixture then stopped at authentication preflight with zero workers; this
does not qualify live upload/embedding. Owned browser and test ports/processes
were closed, and fixture workspace/document/vector rows were zero.
Evidence: `C:/Users/Ninkear/.codex/tmp/shared-p04-settings-20261005/P04-REPORT.md`.

## Committed and Running

Reviewed fixes committed in `6c0fd48`. After verifying the production assistant
idle, its owned root408 was stopped through the CLI and relaunched from the
checkout: launcher parent2808, fresh root18848, creation epoch1791195979.9564.
Healthz returned HTTP200 on7860 and the owned-active-run list was empty.
AnythingLLM Desktop was not stopped. The separate bridge review's native
backend on-disk update still requires its own user-approved Desktop activation;
this qualification does not claim that backend patch is active in memory.

P01/P02/P04/P05/P06/P07 are complete in the shared pool. P03 remains explicitly
deferred pending the recognition-policy decision. No push was made by this chat.
