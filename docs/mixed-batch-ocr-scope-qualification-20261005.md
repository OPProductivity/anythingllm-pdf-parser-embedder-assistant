# Mixed-batch OCR scope qualification

Shared review item P01; baseline `39172f7`. Experiments and retained evidence:
`C:/Users/Ninkear/.codex/tmp/shared-p01-mixed-20261005`.

## Confirmed defect and bounded correction

The September 4 guard (`64b39e0`) compared selected-range `included_words`
with total physical PDF pages to establish document-wide native emptiness.
Appalachian Reckoning has 433 pages and 138,669 native words. Selecting its
opening four pages (256 words) incorrectly promoted four targeted OCR pages
to full-book OCR. Both file orders reproduced it; disposable workers were
cancelled, and their progress/checkpoints retained. These unfinished runs are
not valid timing benchmarks or successful qualification runs.

The guard now requires complete, unique direct PyMuPDF physical-page rows and
counts their text, not selected-range quality. Missing or malformed evidence
cannot establish whole-document emptiness. The existing material-text
threshold, true-empty scan promotion, targeted OCR plan, recognition, UI,
ETA, upload behavior and output cleanup remain unchanged.

## Fresh real-PDF results

Automatic preparation used original All sources PDFs, separate source copies,
private profiles and fresh cancellable workers. Local-only processing completed
the actual preparation/export stages; it made no embedding/provider requests.
Every completed trial verified original source hashes and the production
workspace/document/vector counts and configuration hash unchanged.

| Case | Seconds | Result |
| --- | ---: | --- |
| Ruggiero/Mullins unshared native control | 55.737 | successful |
| Ruggiero/Mullins shared native | 46.464 | successful |
| Mullins/Ruggiero reversed native | 45.928 | successful |
| Ruggiero/Harkins scan-backed usable text layer | 54.496 | successful |
| Harkins/Ruggiero reversed | 49.284 | successful |
| Corrected Ruggiero/Appalachian Reckoning, end page 4 | 57.967 | successful |
| Corrected reversed mixed order, end page 4 | 57.883 | successful |
| Explicit OCR baseline, Ruggiero/Harkins | 90.872 | successful |
| Explicit OCR candidate, Harkins/Ruggiero | 92.315 | successful |

The mixed book OCR remained scoped to physical pages 1, 92, 238 and 386.
The native article independently resolved the fast strategy. Native runtime
sharing and reversed order preserved metadata, canonical transcript and every
page-parent payload byte-for-byte (13 native, 18 raster, 6 selected mixed,
and 18 forced-OCR files compared). OCR timings vary; no speed claim is made
for the forced-OCR controls.

Two harness issues are excluded, with artifacts retained: an early wrong
Advanced backend label did not exercise forced OCR; an overlong experimental
root was rejected before preparation. An explicit-OCR import under an empty
isolated profile waited on third-party NLTK bootstrap; the owned trial was
stopped, and subsequent profiles used a private copy of installed language
data, without changing production runtime assets.

## Breadth and regression checks

The guard-only corpus check covered 44 original PDFs, 3,484 pages, full ranges,
and first-one/first-four-page selections. Whole-document decisions matched the
baseline for every source. Thirty-three short-range cases no longer falsely
promoted a text-native document to full OCR. This is guard qualification, not
a fresh OCR/embedding replay of all 3,484 pages.

`qualification.json` maps source filenames, ranges, counts and decisions;
case reports retain profiles and dereferenced summaries. Artifact comparisons
use source SHA-256 and semantic file identity because Windows path-budget
shortening legitimately changes physical directory/filename lengths.

Candidate regression tests: 932 pipeline/worker tests, 25 subtests; 45 native
batch/runtime and state-policy tests. The focused guard tests include true-empty
scans, a narrow book range, invalid/missing/duplicate physical-page evidence,
and a layout-OCR peer that must not replace direct native authority. One
existing fixture was made internally consistent: its 2,200-word quality claim
previously accompanied only 36 words in its physical-page rows.

The integrated checkout repeated the mixed original-PDF Automatic run
successfully in 59.319 seconds with production and original files preserved.
The integrated gate passed 977 tests and 25 subtests; Ruff and Pyright passed.

The final fresh live continuation used the committed `76f85e9` snapshot,
original Ruggiero and Appalachian Reckoning PDFs, selected opening ranges,
and a separate native AnythingLLM backend with empty storage. It completed
in 59.866 seconds: five selected records, five physically confirmed vectors,
clean integrity audit, and a successful manual retrieval request (HTTP 200,
five results). Evidence is retained in `l/p01-fresh/report.json`.
After owned backend shutdown, all test workspace/document/vector/API-key rows
were removed, together with the test namespace, vector-cache entries and
uploaded documents. Original sources, research storage and installed backend
hash remained unchanged. Production assistant activation is coordinated
separately from experimental qualification; Desktop was never restarted.

Coordinator activated the stable combined checkout after the bridge review's
source integration: ownership-verified assistant-only restart changed root PID
8068 to 408 (launcher parent24436), healthz returned HTTP200 on7860. The Desktop
process was not stopped. Later reviewed corrections receive a separate gate,
commit and activation record.
