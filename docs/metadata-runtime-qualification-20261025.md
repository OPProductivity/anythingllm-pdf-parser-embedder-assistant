# Metadata runtime qualification, 25 October 2026

Status: qualified for publication. Complete-corpus runtime and evidence checks
and final integrated regression tests passed. The 1.2x aspirational target
remains unmet.

## Objective and comparison

Reduce fresh metadata acquisition and inference cost toward 1.2 times the original
runtime control, without losing the current author-recognition results. Ordinary
neutral filenames with retained internal metadata are the primary accuracy
condition. Pure-neutral and original-filename results must also be preserved.

The complete retained suite contains 1,150 presentations of 382 reviewed PDFs:
386 original-name presentations (including four deliberate aliases), 382 neutral
filenames with embedded metadata retained, and 382 pure-neutral presentations.
The retained candidate24 reference matches are 351/386, 317/382 and 292/382.
These are matches against the retained expected complete rosters. This speed
experiment is not a new manual validation of all author ground truth.

The full experiment processes every presentation independently with three frozen
versions, cycling through all six execution orders:

1. The original control, `baseline-source-3bc-matched14`.
2. Current main at `9824c7a1426f2f1a65418aa6523d9f5c089c685d`.
3. The optimized candidate.

The fresh original-control ratio measures progress toward 1.2x. A separate fresh
candidate/current-main ratio establishes whether this change actually reduces
runtime on the same machine. Historical timing ratios use different runs and
must not be substituted for either fresh comparison.

## Changes under qualification

- Forward native text already read during the current collection through the
  native assessment and OCR-informed planning pass, rather than extracting the
  same first five sorted pages three times. Failed reads are not stored as
  successful evidence and retain their later retry opportunities.
- Let rights reconciliation and the portable-book expert consume the same
  acquired native text and layout. Standalone calls retain real PDF extraction
  fallbacks. No evidence persists into the next PDF or inference call.
- Parse explicit copyright rosters once per book-expert invocation, then compare
  each candidate against those same ordered rosters.
- Keep author-separator and responsibility-boundary semantics while preventing
  repeated regex scans from every position in long whitespace runs. Consecutive
  punctuation, wrapped rosters and internal surname hyphens remain supported.
- Transfer the identical rendered RGB pixels to Tesseract as lossless PPM rather
  than compressing them into PNG. Keep all page regions, scales, preprocessing,
  segmentation modes, retry rules and OCR worker limits.
- Start recognition as each raster becomes available within the existing bounded
  batch. Only the caller thread accesses PyMuPDF. Unexpected later rendering
  errors release workers waiting for a retry raster before executor shutdown.
- Read `Pixmap.samples` once per raster instead of making repeated full copies.

There are no new reusable PDF, OCR or author-result caches, GROBID calls, skipped
experts, narrower evidence plans, weaker recognition thresholds or changed role
priorities. The laboratory still receives its broader acquisition plan.

## Evidence and test boundaries

Before the full run, the six targeted PDF presentations produced identical
author decisions and decision traces against frozen main, excluding collection
duration. Those diagnostic cases do not establish a corpus-wide speed result.

The final integrated offline suite completed with **3,337 passed**, one skipped,
43 deselected and 28 passing subtests. Four initial failures were stale UI output-count assertions
also reproduced against frozen main: the recent editable overrides increased
reset callback outputs from 45/46 to 49/50. The assertions now cover the four
preserved override editors. The focused 29-test acquisition check also exercised the
final cancellation guard, image-pixel identity, standalone extraction fallbacks,
bounded OCR workers, caller-owned rendering and laboratory acquisition.

Qualification compares every candidate result to fresh current main, checking
complete author rosters, all decision traces except collection duration, native
and opening plans, OCR tasks and attempts, collection completeness and scheduled
expert completion. Labels are confined to the controller; workers receive only
the input path and job number. Each timed call acquires native and OCR evidence
anew. Mains power and disabled battery saver are checked before and after each
call. Source hashes are frozen and checked again at completion. The PDF input
manifest includes SHA-256 and file sizes and verifies all 1,150 PDF headers.

The 7860 server was idle, with no owned active runs, before being temporarily
stopped for the full measurement. No simultaneous PDF rendering, offline tests,
preview or ingestion jobs are part of the experiment. OS disk caches are not
flushed. The test covers the metadata sampling/acquisition/inference path, not
full document extraction, upload, embedding or retrieval. Full-document page
geometry enumeration, override application and UI event latency are outside the
timed recognition benchmark; the inference functions themselves are the ones
used by production preparation.

Local experiment artifacts are retained under the Codex project directory at
`outputs/metadata-speed-20261025/`, including the controller, source snapshots,
corpus and source manifests, diagnostic profiles, JUnit reports and complete
per-presentation results. The older qualification artifacts remain intact.

## Final qualification

All 1,150 presentations completed with all three frozen versions: 3,450 fresh
inference calls. Every optimized author verdict and decision trace matched
fresh current main, excluding collection duration. There were zero processing
exceptions, failed acquisition attempts, changed acquisition traces, changed
evidence plans or unfinished scheduled experts. All source hashes passed the
after-run check and every input PDF retained its pre-run SHA-256.

| Filename condition | Fresh current main | Optimized | Runtime reduction from current main |
| --- | ---: | ---: | ---: |
| Original | 352/386 (91.2%) | 352/386 (91.2%) | 4.1% |
| Neutral, embedded metadata retained | 318/382 (83.2%) | 318/382 (83.2%) | 3.6% |
| Pure-neutral | 293/382 (76.7%) | 293/382 (76.7%) | 2.9% |

The fresh main scores are one match higher in each condition than the older
candidate24 reference. Those three additional matches are the existing HubSpot
corporate-author behavior in main, not an accuracy gain introduced by this
speed change. Historical and fresh main also have a few diagnostic-trace
differences from intervening fixes; the optimized candidate matches fresh main
exactly throughout the complete suite.

| Frozen version | Sum of 1,150 timed calls | Mean per presentation | Median per presentation | Runtime / original control |
| --- | ---: | ---: | ---: | ---: |
| Original control | 1,927.2546 s | 1.6759 s | 0.9568 s | 1.0000x |
| Current main `9824c7a` | 2,556.5492 s | 2.2231 s | 1.5649 s | 1.3265x |
| Optimized candidate 02 | 2,466.2971 s | 2.1446 s | 1.4582 s | 1.2797x |

The optimized version uses **3.53% less recognition time** than contemporaneous
current main, saving 90.25 seconds across the suite. Its observed median is
6.82% lower. The entire three-version experiment took 6,960.75 seconds of wall
time; the table reports each version's timed recognition calls, not experiment
wall time or end-to-end application processing.

The paired grouped-bootstrap 95% interval for optimized/current-main runtime is
0.9445-0.9847. All three filename conditions and the deliberate aliases stay
with their PDF review ID during 3,000 resamples of the 382 IDs. This describes
the observed document mix, not hardware or run-to-run variability. The result
is one complete balanced comparison, with no cherry-picking of partial timing
runs or removal of slow PDFs. It qualifies as an improvement under the user's
aspirational 1.2x goal. It does not meet 1.2x: that would require about another
6.2% reduction at the observed original-control time.

This experiment measures the combined change, not the isolated benefit of each
individual edit. Post-run profiling still finds repeated lexical interpretation
of book credits, long whitespace scans in another responsibility suffix rule,
and repeated native-field geometry work. Those are further optimization
opportunities; they have not been changed or qualified in this release. No
reduction of the OCR evidence plan is used to close the remaining gap.

The machine's background OS activity was not controlled. Source isolation,
rotating execution order and per-call power checks reduce avoidable comparison
bias but do not make these timings a universal hardware constant.

The compact publication audit is also saved beside this document as
`metadata-runtime-qualification-20261025.json`. Complete per-presentation
evidence, source manifests and input hashes remain in the local experiment
directory. The production source comparison checked 106 runtime Python files
against the frozen candidate, allowing only platform newline normalization;
all matched.
