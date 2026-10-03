# Isolated OCR qualification, October 3, 2026

Production OCR has not been modified. Original PDFs were only read. No
AnythingLLM documents, vectors or workspaces were created during this study.

## Corpus and Scope

The recursive census covered the Film II Articles and MMT Keywords Resit sources
folders: 138 file paths, 134 byte-distinct PDFs, 7,433 pages by path and 6,987 unique
pages. All PDFs opened successfully. A scan-backed page is operationally defined
as having a raster image covering at least 65% of the page; a usable embedded
text layer is not an exclusion. This is a reproducible image-placement heuristic,
not a manual classification of every page or a guarantee for tiled/inline images.

There are 1,729 unique scan-backed pages. Twenty-three unique PDFs have scans on
at least half their pages; none have the 20-49% substantial-minority proportion.
The user deferred exhaustive fresh OCR of full books after a 2-4 hour estimate.
The first fresh article/excerpt pass covers 335 scan-backed pages in 24 PDFs of
100 pages or fewer. Longer books are not exhaustively fresh-OCR-qualified.
All 335 expected pairs completed, with no missing receipts or captured failures.
Fifty pages used photographed-page crop OCR and 285 used Unstructured elements;
48 paired page texts changed. These counts do not measure correctness.

## Native-Body Replacement Route

Only two unique PDFs (six pages) satisfy the existing marginal-annotation plan
plus page-sized raster condition. Fresh cropped recognition was run at both
PSM 4 and PSM 6 on all six, using the actual existing acceptance gate.

- Garncarz page 7: current PSM 4 moves reference-title continuations and journal
  names out of order. PSM 6 preserves those continuations. Both have 532 words,
  near-identical alphabetic coverage and improved noise, so the gate cannot tell
  them apart. Both crops omit visible reference numbers and retain spelling errors.
- Garncarz page 1: existing recognition already selects PSM 6. Its crop correctly
  excludes the neighboring-page sliver; preserving this behavior matters.
- Handbook of Latinos and Education page 10: the existing crop cuts real contents
  text at both edges, including chapter/page numbers and title endings. Both modes
  inherit this error and pass the acceptance gate. Whether it reaches a final run
  depends on front-matter/page inclusion; do not infer that every handbook run does.
- Handbook pages 11 and 12: cropped alternatives lose content, but the gate rejects
  them and keeps native text.
- Handbook page 304: the rendered page is blank apart from reverse-page bleedthrough.
  Its embedded text is noisy; fresh alternatives are empty or very short and rejected.
  This is a native-route risk, not proof that automatic backend selection uploads it.

All six complete page renderings were visually reviewed against candidate text.
Three of six current replacement results pass the gate. The specific confirmed
Garncarz reading-order defect occurs in 1/134 unique PDFs (0.75%). Including the
different, conditionally included handbook crop defect gives 2/134 (1.49%). These
are confirmed route-specific findings, not the prevalence of all OCR errors.

## Broader Candidate and Tradeoffs

Each fresh page pair uses the actual `_unstructured_one_page(..., 'ocr_only', ...)`
production route. The isolated candidate changes only resolved recognition PSM 4
to PSM 6 in its worker process, retaining crop, model and downstream processing.
Unstructured fallback recognition is deliberately unchanged; identical output on
that route is not evidence of improved accuracy.

Morin page 11 demonstrates why a blanket single-block substitution is unsafe:
its word count rises from 491 to 672, but the additional opening text includes
gibberish recognized from the photograph, and printed columns are interleaved.
Morin page 1 also interleaves columns in both versions; the candidate fragments
the word `Observateur` and does not solve the multi-column reading order.
These complete source renderings were visually checked. A word-count increase
does not imply recovered content, and a decrease does not itself prove a loss.

The viable direction is route- and geometry-specific recognition plus content-safe
crop validation, not globally forcing PSM 6. No candidate is promoted here.

## Reproduction and Evidence

`experiments/ocr_corpus_qualification_20261003.py` contains census, route comparison,
fresh pair, benchmark, rendering and report stages. Receipts are resumable and
store source paths, page numbers, actual OCR route/crop/recognition evidence, both
texts, elapsed times and errors. SHA-256 directories only deduplicate source PDFs;
receipts contain readable source identities, not unresolved hash-only references.
Baseline recognition runs first within each pair; timings are observational and
can reflect warm-up/cache effects, not a controlled candidate-speed comparison.

Evidence root: `tmp-output/ocr-corpus-20261003`. The inventory, per-PDF census,
native-layout baseline, six native-body comparisons and fresh per-page pairs are
preserved. `article-comparison.csv` lists completed pairs; its summary explicitly
lists missing/error pages until completion. There is no human-transcribed ground
truth for every article page, so this is not a corpus-wide character-error rate.
