# Native batch runtime qualification (2026-10-04)

Baseline: `ac85b6ab115f5e75bb051a0e58dca17c5edc0a0f`.

## Scope

Automatic/auto batches of at least two PDFs may reuse the first successful
worker's optional Unstructured capability observation. Eligibility requires a
matching, deferred native-text preflight with verified full native coverage,
no likely scanned documents, no targeted visual-text pages, and no deep
extraction. Other routes retain their previous behavior.

The first existing cancellable worker performs the real probe. Successful
worker-result handoff carries the observation through the existing run-local
batch context. No extra process or cross-run cache is introduced. Each later
worker still initializes its own Tesseract environment and checks availability.
The shared snapshot excludes `ocr_required`; document-specific strategy
resolution remains independent. Profiles record whether a probe was reused.

OCR algorithms, UI, ETA, security, uploads, embedding, vector confirmation,
cache matching, and output/private retention policies are unchanged.

## Fresh preparation comparisons

Eight academic PDFs were tested in four two-PDF batches, twice with reversed
source/arm ordering. Settings covered page-preserving segmentation, whole-file
mode, front/back-matter inclusion and omission, a smaller segment target,
inline citation fallback, and bounded book page ranges.

| Two-file settings | First round saved (s) | Reverse round saved (s) |
| --- | ---: | ---: |
| Page-preserving, default settings | 5.189 | 5.586 |
| Whole file, omit front matter | 4.250 | 15.574 |
| Page-preserving, target 750, omit back matter, inline fallback | 2.590 | 4.308 |
| Books, pages 3-12, omit front/back matter | 2.820 | 0.997 |
| Eight-file aggregate | 14.849 | 26.465 |

The 15.574-second result includes a baseline first-worker timing outlier.
Book-only gains are small and variable; this is not a universal five-second
guarantee. Both aggregate sets exceeded the requested 12-second/eight-file
threshold. A final variant retaining local Tesseract initialization passed two
additional paired default comparisons (21.660 and 9.579 seconds saved); host
timing was inflated, so these are not a typical-speed estimate.

All eight matrix comparisons and both final comparisons matched decoded
segment, page-parent, transition and provenance manifests, metadata,
upload-row text, and exported TXT content. Canonical transcript references
resolved to matching bytes. Workers were fresh subprocesses; generated outputs
were removed between comparison arms, and source PDF hashes were unchanged.
Preflight inputs were held constant. These are preparation-stage tests, not a
fresh provider-side embedding benchmark; no embeddings or workspaces were
created.

## Harness corrections

An initial partial comparison saved 1.161 seconds, below the target; subsequent
complete repeated comparisons are reported above. An incorrect manifest key
and unsupported whole-file setting were corrected in the harness. A strict
book comparison then exposed established filename identity behavior: long
canonical filenames incorporate the actual output directory into their hash.
Both arms were therefore rerun at the same absolute output path with fresh
files between arms. This was a test-path discrepancy, not text corruption.
An isolated smoke harness also needed to close its own logging handlers before
deleting its temporary profile.

## Regression checks

- Full deterministic offline suite: 2,300 passed, one skipped, 34 deselected,
  15 subtests passed. Three dependency deprecation warnings; no failures.
- Focused runtime-sharing suite: 32 passed, including eligibility exclusions,
  unavailable backends, independent OCR selection, local environment setup,
  JSON handoff without credentials, and no cross-run snapshot inheritance.
- Actual Automatic parent plus real child workers, isolated prepare-only run:
  successful, two ready PyMuPDF sources, reused-probe flags `[false, true]`,
  zero provider calls and zero uploads.

The parent smoke covers the production wiring, not only helper tests. Existing
offline tests cover adjacent extraction, worker cancellation, cache and vector
confirmation paths. No regression was found within this qualification scope.
