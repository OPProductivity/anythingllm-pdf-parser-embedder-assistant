# Further pipeline work and the TOC III timing workload

This candidate starts from the qualified production release `826cf03`.
Production on port 7860 remained running during the isolated experiment.

## Narrow changes

- Group OCR word rows by their existing block/paragraph/line keys in one pass,
  rather than filtering every row again for every line. Input order within keys,
  physical-line bands and all subsequent ordering are unchanged.
- Resolve a native Document's immutable name once on first use. A different
  Document or changed name is checked again; source-version, full-content,
  encryption, dirty-handle and page-geometry checks remain.
- Anchor a canonical artifact catalog at its already resolved root, while still
  resolving and validating every actual target. Public standalone role checks
  retain their original root resolution and containment checks.
- A TXT-only successful-retention catalog does not instantiate the unused
  manifest text-pool writer. Ordinary catalogs remain eager, and a deferred
  catalog still validates the pool before any JSONL use. Existing manifest rows
  are read and validated independently before retention.
- Advisory selection ETA samples three pages rather than 24. Required all-page
  text coverage and author experts' evidence acquisition remain separate and
  unchanged. Confirm-time ETA already uses the exact-version preflight facts.
- A source-boundary capacity check recreates a missing configured public output
  directory before inspecting its actual volume again. Other creation/inspection
  errors become a safe hold with unavailable free-space evidence. Earlier
  preparations remain intact and no upload begins on an unknown capacity result.

There is no persistent/cross-request PDF or OCR cache, no GROBID, no change to
recognition experts, their priorities, OCR regions or retry policies, and no
reduction of exact vector confirmation requirements.

## Qualification

Ruff and core Pyright pass. After the capacity repair, the complete offline suite
passes 3,535 tests and
28 subtests; two tests are skipped and 43 are deselected by the established
offline configuration. New tests cover all-page coverage versus advisory ETA,
pool validation, per-target containment checks, native Document-name rechecks
and exact split-block OCR-line ordering.

Both current production and candidate use their native measurement collectors
in the 31-source / 2,951-page native comparison. Every raw text, transformed text
and layout-evidence digest matches, as do body/margin OCR-call counts. Only the
measured targeted-OCR and subprocess-duration fields are excluded from evidence
equality. This exercises native body extraction/layout, not complete author
inference or Unstructured for that historical set.

Three course-PDF pages are rendered and freshly OCRed. Old and new line
reconstruction match exactly on the full recognized word rows and separate
left/right subsets. No raw page images, TSV or text are added to the receipts.

The 42-source private-retention replay uses all 5,420 archived prepared segments,
including existing manifest-text pools. Complete export-name/content digests
match. Observed retention time is 17.658 seconds for production and 13.087 for
candidate; both avoid temporary segment writes. Fixture creation, PDF processing,
API operations and public publication are excluded. These observations overlap
other qualification work and do not establish a controlled whole-ingestion
speed ratio or explain the entire historical finalization window.

## Corpus and coverage boundary

The new timing workload selects 31 distinct original PDFs / 1,683 pages from
TOC III's Articles Week 1 through Articles Week 13 folders. There are 34 distinct
available PDFs. Every nonempty week is represented; remaining selection uses a
fixed content-hash ranking independent of OCR difficulty and recognition outcome.
Root-level PDFs are excluded. Original bytes are hashed before and after.

The actual factory-default Automatic callback uses full sources, blank overrides,
the default first-five metadata preview, fresh confirmed preparation, a new
workspace, real embeddings and exact vector confirmation. Browser transfer and
rendering are excluded. Short-lived development state is isolated from the
running production assistant. Workspace creation and upload respect the shared
cross-process AnythingLLM mutation lease; any resulting wait is separately timed.
The isolated assistant history does not reproduce the cost of a large production
run-history index.

This course corpus has appeared in earlier metadata work. It is a different
timing workload from the thesis ingestion, not an untouched accuracy holdout.
There is no new manual author-correctness audit or 1,150-presentation metadata
benchmark. Previous recognition percentages are reference values, not newly
measured accuracy. No new publisher rules or workbook priorities are introduced
by these mechanical changes.

The first real attempt was interrupted after 20 preparations / 745.697 seconds:
the empty public output directory had disappeared and the next disk-usage query
raised FileNotFoundError. The removing actor was not established. The interrupted
workspace was independently verified to contain zero documents, with no upload
report or embedding ledger. Its artifacts and receipts are retained; the fresh
qualification does not reuse any prepared, author or OCR evidence from it.

## Completed fresh corpus run

All 31 source transactions reached `exact_vectors_proven`; all 31 preparation
receipts were ready, with zero upload errors. All 1,659 records were newly
attached, queued and exactly vector-confirmed. Cache eligibility/reuse and
pre-existing workspace record counts were all zero. Original source hashes and
the measured candidate Python source digest were unchanged.

Total measured time was **1,736.351 seconds (28m 56.4s)**. This includes the
default first-five preview and all confirmed full-source processing, excluding
browser transfer/rendering, initial imports and final audit reads. Mean total
time per selected PDF was 56.01 seconds; actual worker preparation averaged
33.32 seconds with a median of 28.21 seconds.

| Non-overlapping phase | Seconds |
|---|---:|
| First-five metadata preview | 9.819 |
| Selection advisory ETA | 0.163 |
| New workspace creation | 0.308 |
| Confirmation coverage/preflight | 7.124 |
| Setup to first worker | 0.773 |
| Local preparation, including author recognition and review | 1,040.530 |
| Upload, embedding and exact vector confirmation | 666.425 |
| Finalization after upload | 11.209 |

The two shared-mutation-lane waits together were below 0.001 seconds. Within
preparation, initial metadata/author inference took 96.845 seconds, native
candidate processing 140.441, Unstructured candidate processing 182.850,
independent author-field review 520.357 and packaging/diagnostics 16.645.
Candidate processing includes its cleanup, segmentation and candidate exports;
Unstructured duration is not equivalent to pure OCR duration. Other phases,
worker startup/control and boundary work account for the remainder.

The nine sequential embedding-group windows totaled 569.444 seconds. Another
96.981 seconds covered attachment/setup and other upload work outside them.
Reconciliation within those windows took 305.092 seconds, including queue waits;
actual measured vector-store observation was only 16.221 seconds, or 2.43% of
the upload window. These are nested observations, not additive phases or an
independent measurement of provider-only compute/network duration.

Finalization included 5.100 seconds of private retention, 3.554 of public export
publication and 1.533 of published-text separation. The full tail was 11.209
seconds. These observations do not reconstruct unmeasured historical subphases.

The machine reported AC power throughout, no battery saver, and 18.69% average
whole-system CPU activity. Parent Python CPU excludes preparation/OCR workers
and AnythingLLM and is not used to invent a load-corrected runtime. This is a
different workload from the thesis runs and establishes no new whole-corpus
1.2x ratio or author-recognition accuracy score.

The 20 overlapping fresh preparations before and after the capacity repair
matched on selected author, title, backend and page count. That is a stability
check, not manual authorship validation or a complete output comparison.

The compact aggregate receipt is `toc-pipeline-qualification-20261026.json`.
Detailed local receipts, the per-PDF CSV and timing report are retained in the
Codex workspace's `outputs/toc-pipeline-qualification-20261026/qualified-run`.
