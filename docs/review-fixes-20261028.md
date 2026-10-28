# Bounded workflow review fixes — 28 October 2026

These corrections follow independent checks of seven reported application defects.
The first-pass review covered all 794 required text files at their captured
hashes; eight files changed afterward and were reviewed again for these fixes.
The bounded second and third passes are not a whole-code security certification.

Creating a new workspace now requires the local output-capacity check to pass
first. Later exceptions and pre-upload holds distinguish workspace creation from
document submission, identify a successfully created workspace, and retain it for
an explicit retry/reuse decision. A precheck cannot guarantee that space will
remain available, so later capacity checks remain in place.

Cancellation receipts describe confirmed selected records separately from newly
attached records. Existing workspace vectors legitimately contribute to the
former; they must not be used as a numerator over the latter.

An explicit lower page-preserve ceiling reaches the local segmentation hard
limit and parent-representation harmonization. Other modes retain their passage
target semantics, and the inherited Desktop/embedder upper bound remains in
force. This does not change AnythingLLM settings.

The storage baseline inspector reports a missing LanceDB directory or manifest
without connecting to and initializing it. Normal existing-store inspection
remains available. This is a missing/uninitialized-path correction, not an
OS-enforced read-only sandbox for every possible library/filesystem condition.

Normal and exactly reconciled file attachments apply the same guarded requested
folder handling, including per-source destinations. The final moved location is
recorded in the attachment receipt. Unsafe absolute locations and occupied
targets are rejected, while a recovered file already at its destination is
accepted. A failed relocation becomes an explicit error rather than a blind
re-POST or a successful queue claim.

Duplicate skip decisions require linked vector IDs to be present in the selected
physical workspace namespace. SQLite mappings remain useful inventory evidence,
but cannot alone establish searchability. If existing mappings lack physical
proof, the batch is held before attachment; it neither skips the content as
complete nor automatically re-uploads it. An unreadable existing inventory
also holds submission. A missing local SQLite database now holds skip-mode
submission too: the live API can still report a workspace when the local
storage path is missing or wrong. The user-selected upload-again policy
remains a separate explicit path. The
lookup reads only the requested ID column in bounded ID batches and does not
accept another namespace as proof.

The short-OCR display predicate no longer contains an unreachable line-count
guard after whitespace normalization. This deliberately preserves its existing
acceptance behavior: stacked titles can legitimately contain many short lines.
No stricter raw-line cutoff or new OCR acquisition policy was introduced.

## Qualification boundary

Regression coverage is in `tests/test_review_closeout_regressions.py`, together
with adjacent existing tests. Checks use synthetic page text, mocked external
responses and owned temporary SQLite/LanceDB stores. They include a real local
LanceDB lookup across more than one ID batch, mixed existing/new cancellation
counts, normal and recovered attachment layouts, relocation failure, and a batch
hold before any upload when vector proof is missing. A real two-page local PDF
preparation and worker JSON serialization check the page-ceiling path through
packaging, without submitting it to Desktop.

On the settled candidate, the focused parser/worker checks passed **52 tests**;
the full offline parser suite passed **3,554 tests, 29 skipped, 43 deselected,
and 28 passing subtests**. Ruff and Git whitespace checks passed. The private
seven-page production OCR pack passed against its retained baselines; that is
change detection for those pages, not full-document or author-accuracy proof.
The WSL retrieval CLI correction belongs to a separate local repository.

No live Desktop import, paid embedding, full 1,150-presentation recognition
benchmark or new performance qualification was run. The selected performance
implementation remains the basis; these changes do not restore the reverted
scheduler experiments or alter author expert priorities. A source checkout
change is not evidence that an existing server is running this revision.

## Missing-database follow-up qualification

A later review found that live API workspace readiness can succeed even when
the expected local SQLite file is absent. The earlier missing-database
exception to the duplicate audit therefore did not provide a safe default
skip-mode decision. The upload coordinator now holds before attachment for
`missing_db`, as it already did for unreadable existing inventories and missing
physical vectors. Older transport tests that intentionally use no database
now select the explicit upload-again policy; two new regressions cover the
missing-database hold and the API/local-storage mismatch.

The revised offline parser suite passed **3,556 tests, 29 skipped, 43
deselected, and 28 passing subtests**. A separate live Desktop check used a
synthetic two-page PDF, a newly created workspace, and the production file
upload and embedding path. Both selected records were uploaded, embedded and
exactly vector-confirmed; a subsequent duplicate audit found them in the
selected physical namespace. The owned workspace, documents, namespace and
temporary credential were removed and verified absent. The pre-existing
workspace, document and vector-mapping counts were unchanged. This is one
small live path check, not a large-workload performance or author-accuracy
qualification. The missing-database hold itself was exercised with a mocked
transport, not by altering live Desktop storage.
