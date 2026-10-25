# Recognition effort and chapter geometry: completed qualification

Source 03 was frozen before the full comparison. The reference is main
b32aa1f; its recognition implementation was already qualified before the
reporting-only commit. Release receipts separately record publication and the
running server. No new optimization cycle is started by this closure.

## Population and recognition

The suite contains 382 distinct PDF byte streams, four extra original-name
aliases, and 1,150 filename presentations. Four fresh versions ran for every
presentation: 4,600 metadata-inference calls. All 24 version orders were balanced
within each filename condition. Expected authors were held by the outer scorer;
workers received only a path, job identifier and effort mode.

| Filename condition | Main reference | Extremely Thorough | Thorough |
| --- | ---: | ---: | ---: |
| Original | 356/386 (92.23%) | 356/386 (92.23%) | 356/386 (92.23%) |
| Neutral, internal metadata retained | 321/382 (84.03%) | 322/382 (84.29%) | 322/382 (84.29%) |
| Pure neutral, metadata stripped | 296/382 (77.49%) | 297/382 (77.75%) | 297/382 (77.75%) |

No previously correct presentation was lost in either mode, including against
the earlier qualified results. The permitted five gross losses for Thorough
were not used. The expected-roster comparator is unchanged. Its denominators
include three whole-issue PDFs where a correct abstention is expected, nine
presentations in total; these are not claims of recognizing a personal author.
Initials, surnames and organization aliases can remain incomplete under this
strict comparator without being wrong-person assignments.

Both changed decisions are P313, one PDF in its two neutral conditions.
The complete first-five-page overview was visually rechecked: Chapter 23,
JOSEPH GARNCARZ and its following title belong to the selected local opening,
with distinct later name and title running heads. The neighboring previous
leaf is a different scope. The original-name result was already correct.
The new rule reads these relationships from the document and does not contain
an author-name or corpus-ID lookup.

## Completed runtime review

| Version | Summed wall seconds | Ratio to original control | Ratio to current main |
| --- | ---: | ---: | ---: |
| Original control | 2314.5090 | 1 | — |
| Current main | 2836.1278 | 1.22537 | 1 |
| Extremely Thorough | 2824.4339 | 1.22032 | 0.99588 |
| Thorough | 2790.5038 | 1.20566 | 0.98391 |

Thorough's elapsed sum is 1.6087% shorter than matched main and 1.2013% shorter
than Extremely Thorough. Extremely Thorough is essentially time-neutral
against main; its 0.4123% point reduction is not a robust separate speed claim.
The 1.2x target is narrowly unmet by the point estimates and remains an end goal,
not an absolute rejection threshold. Total application CPU work decreased
2.1076% for Thorough and 0.5893% for Extremely Thorough against main; CPU is
diagnostic, not an additional acceptance penalty.

Eight calls around P233 recorded an unsuitable/transitional power endpoint.
The full results are retained. A matched sensitivity analysis removes all four
versions of all three presentations of that PDF, leaving 1,147 presentations.
It gives 1.20945x for Thorough and 1.22338x for Extremely Thorough against the
control; respective ratios to main are 0.98504 and 0.99638. No replacement runs,
selected-call substitution or synthetic load correction were used.

A 2,000-resample PDF-grouped bootstrap gives a Thorough/main ratio interval
of 0.97512–0.99270, and Extremely/main 0.98817–1.00360. This describes
within-corpus heterogeneity, not independent publisher families, machine/day
repeatability or a thermal/contention correction. Related PDF variants remain
related works. Detailed counter formulas are in
metadata-measurement-review-20261025.md. Wall-time sums remain the speed metric.

## Acquisition and integrity

Both modes retained identical native/opening/OCR plans: 3,346 planned regions,
all primary readings and all scheduled experts. Extremely Thorough retained
3,846 OCR executions, including 614 rechecks. Thorough retained 3,674, including
442 rechecks, and explicitly deferred 172 optional orientation rechecks:
58 original, 57 neutral-retained and 57 pure-neutral. No deferral violated the
selected policy. Planned regions include crops assessed as blank without a
Tesseract execution; region counts are not execution counts.

Extremely Thorough's OCR input/output hash multiset equals reference; Thorough
is a subset, with the primary-reading audit independently passed. There were
no processing exceptions, failed/incomplete expert schedules, missing CPU
counters, unexpected OCR changes, reference author drift, or previously
qualified recognition losses. All four Python sources, helpers and PDF inputs
matched their recorded fingerprints at completion. The observer remained alive.

The earlier source-02 partial run was stopped for a real crop-origin bug,
not a speed judgment: a crop starting inside a page could masquerade as a page
header. Source 03 anchors normalized OCR coordinates to the actual page and
abstains when anchors are missing. The aborted artifacts are preserved and are
not qualification data.

## Functional and review coverage

The final offline suite passed 3,472 tests and 28 subtests, with one skipped
test and 43 explicitly deselected integration tests. Ruff and Pyright passed.
Bounded real checks exercised two scanned PDFs through live preview, eight
independent editors, mode changes during detection, a new private server
restoring a non-default preference, a two-file folder preview, and complete
local preparation of one native PDF in each mode. A real UI Confirm dispatched
the durable worker for one native PDF: Thorough reached the source profile and
nested field review, Publisher override was saved, and blank Author used the
detected Alice Harrington value. That final check also verified the actual
successful UI completion state. Test-harness attempts are preserved separately;
incorrect initial mode timing/completion-string assumptions are not counted as
passing execution checks.

No uploads or embeddings were needed to exercise the changed paths. These
checks do not establish unchanged PDF bytes end to end or freshly verify all
382 expected author labels. The complete inference sweep is not a full corpus
extraction/upload/embedding sweep.

The expanded workbook preserves its 79 historical sheets/cells and styles,
appends the completed runtime/evidence account, and adds the 386-presentation
filename audit plus 31 whole-overview/literal-code reviews. Twenty reviews have
fresh lab prefix trials; other follow-ups explicitly use retained original-name
ledgers. Seventeen external corroboration records distinguish publisher,
published-excerpt and catalogue evidence. Historical ground-truth cells and
the accepted-form comparator were not silently changed. HTML keeps each complete
first-five-page montage visible beside the bounded literal code excerpt.

The six-PDF concurrency comparison remains 24 fresh calls at 1/2/4/8 workers,
with 79 identical OCR readings at each setting. Eight had the shortest observed
sum; the existing cap is retained. It is not a universal machine/batch optimum.

## Fixed behavior and remaining limits

The new chapter specialist joins existing adjudication at priority 116; existing
expert priorities were not reduced for speed. The native field mapper now uses
the current line's actual word indexes for By-line composition. This latter
path is a diagnostic shadow review, not a claimed benchmark author gain.
Reporting fixes preserve labels when groups are empty, include low outliers,
handle zero-OCR axes, validate finite durations/counts, and separate chart footer
text. A worker-set snapshot prevents telemetry registration races. No Python
interpreter defect was diagnosed.

UI first use defaults to Thorough, immediately persisted in its private home;
legacy engine/CLI calls retain Extremely Thorough. A confirmed request freezes
its choice. Laboratory acquisition retains the full schedule. Production has
no cross-request PDF/OCR/author-result cache or shared in-flight author result;
one preview may reuse its freshly acquired report within that same request.
No GROBID is used.

Still open: a calibrated whole-PDF failure/early-stop predictor, independent
work-family holdouts, broader uniform manual source review, stronger parent-work
and publishing-role relationships, and the 96% original-name target. The current
regional policy is not a claim of a greater-than-90-percent PDF failure
probability. All 81 complete canonical personal-name filename clues already
pass; the broader surname group still has incomplete/name-form/abstention cases.
No further optimization loop is started as part of this closing release.
