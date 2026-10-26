# Same-corpus incremental efficiency qualification

This extends `bb0b41d` with three narrow changes:

- Native field-review measurements use a specialized copy for ordinary native
  containers. Proven immutable scalar/coordinate-tuple leaves may retain their
  identity. Every mutable container remains separately copied, including shared
  aliases and cycles. Unknown extension types and non-string dictionary keys use
  the existing deepcopy protocol. Source/geometry/read-option guards are retained.
- The opening and OCR field maps use the existing invocation-local lexical
  context for repeated normalization and name-shape parsing. It expires when the
  map finishes. Every grouping, role interpretation, genre assessment and expert
  verdict still executes independently. No evidence or verdict cache is added.
- The two photographed-crop OCR helpers serialize their already preprocessed
  grayscale raster as lossless PGM instead of PNG. This avoids compression;
  pixels, resolution, crop, model, segmentation options and retry policies remain
  unchanged. Temporary files retain their existing managed lifetime.

There are no concurrency changes, evidence cuts, weakened vector checks,
GROBID use, publishing-rule/priority changes, or persistent document/OCR caches.

## Preserved behavior

Ruff, core Pyright and the complete offline suite pass: 3,540 tests plus 28
subtests, with two skipped and 43 deselected. New tests exercise real native
container values, mutable independence, aliases/cycles, extension fallback,
lexical-context expiry and exact OCR crop pixels/options.

Twelve fresh real OCR comparisons preserve decoded input pixels, recognized
text and word geometry exactly. Two direct diagnostic field reviews preserve
their complete reports and OCR call lists. Their profiled durations are not used
as performance claims because profiling overlapped qualification work.

The repeated 31-PDF / 1,683-page default full-source run preserves:

- all 1,723 exported TXT filenames and complete contents;
- all 1,659 uploaded record contents, citation fields, optional Publisher fields
  and text-size estimates;
- all 31 selected metadata records, initial author-inference reports and retained
  field-review reports;
- embedding configuration, chunk simulation, embedder policy and selected-output
  OCR-use flags for every source.

Only measured acquisition/OCR durations and new upload IDs, ingestion timestamps
and temporary upload URLs are excluded from those comparisons. Original PDFs
and the measured production source digest remain unchanged during the run.
All 31 transactions reach `exact_vectors_proven`; all 1,659 records are newly
attached, queued and confirmed, with zero cache eligibility/reuse and zero
pre-existing target-workspace records. The replacement workspace is retained.

The original test workspace and its vector namespace, 1,659 unshared document
records/cache entries and SQL vector rows were removed first. A timed-out
100-record detach request was reconciled from actual state before continuation;
it was not replayed. Native workspace deletion then removed the remaining
namespace. Only audited exact locations were permanently removed. Original PDF
files, old run evidence and unrelated workspaces were preserved.

## Observed timings and attribution

| Non-overlapping part | Baseline seconds | New seconds | Observed reduction |
|---|---:|---:|---:|
| First-five metadata preview | 9.819 | 8.355 | 14.91% |
| Selection advisory ETA | 0.163 | 0.156 | 3.97% |
| Workspace creation | 0.308 | 0.340 | -10.62% |
| Confirmation preflight | 7.124 | 6.617 | 7.11% |
| Setup to first worker | 0.773 | 0.754 | 2.56% |
| Local preparation | 1,040.530 | 1,032.278 | 0.79% |
| Upload/embedding/exact confirmation | 666.425 | 347.733 | 47.82% |
| Finalization | 11.209 | 11.711 | -4.47% |
| Total | 1,736.351 | 1,407.945 | 18.91% |

Shared-mutation-lane waits were below 0.001 seconds in both runs. Short timers
have too much relative noise for their percentages to establish meaningful gains.

The new run takes 23m27.9s versus 28m56.4s. Local preparation improves by only
8.251 seconds. Author-field review takes 510.616 versus 520.357 seconds (1.87%
shorter); its measured Python CPU falls from 172.841 to 167.470 seconds (3.11%).
Native candidate processing takes 135.953 versus 140.441 seconds (3.20% shorter).
Initial metadata/author inference is slower: 102.558 versus 96.845 seconds.
Unstructured candidate time is essentially flat, 183.389 versus 182.850 seconds.

No upload/embedding or confirmation implementation changes here. Its large
reduction is observed service/environment variation, not an attributable code
gain. The nine queue-group windows take 229.568 versus 569.444 seconds, while
attachment/setup/other work outside them grows from 96.981 to 118.165 seconds.
Actual vector-store observation takes 15.755 versus 16.221 seconds, nested inside
reconciliation. These windows do not independently isolate provider-only compute.

An alternating in-memory copy microbenchmark on actual native measurements
observes 24.7% shorter dictionary copies, 39.1% shorter word-list copies and
53.0% shorter trace copies. This demonstrates a component efficiency improvement,
not a corresponding whole-pipeline percentage.

Per-PDF preparation averages 33.054 versus 33.317 seconds; the median is 27.441
versus 28.210. Eighteen preparations are faster and thirteen slower. The maximum
falls from 124.769 to 114.630 seconds, while upper quartile/p95 increase slightly.

## Measurement inventory and limits

The local comparison report covers every whole-run phase, worker phase,
bounded native/layout/decode/cleanup/segmentation/parent/payload/artifact-writing
self/inclusive/CPU timer, parent finalization function and embedding queue-group
timer. It also retains each PDF's timings, worker imports/control, distribution
statistics and recorded system power/RAM/process/service/CPU context. Its CSV has
2,973 numeric comparison rows and includes slower/missing measurements.

Both runs use the same files, order, original filenames, defaults and embedding
configuration, with fresh embedding work. This is one sequential before/after
comparison, not randomized repeated trials. OS cache state, provider load, PC
activity and unmeasured thermal behavior remain possible influences. Parent
application imports and browser transfer/rendering are excluded; worker imports
are included. Parent Python CPU excludes worker/OCR/AnythingLLM CPU, and worker
Python CPU excludes OCR subprocesses. No load-corrected duration is invented.

Exact retained author outputs establish stability on this corpus, not a new
manual authorship audit or remeasurement of the 1,150 filename presentations.
The publishing-knowledge workbook and expert priorities are unchanged.

Detailed local reports and CSVs are under the Codex workspace's
`outputs/toc-speed-comparison-20261026`. The compact receipt is
`toc-speed-comparison-20261026.json` beside this document.
