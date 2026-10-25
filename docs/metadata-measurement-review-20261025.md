# Metadata measurement and OCR concurrency review

Elapsed latency is the speed measure. Total CPU work is diagnostic and is not
an additional acceptance penalty. The completed matched run remains 1.21498
times the original control and 4.4427% shorter than the then-current reference.
Its recognition counts are 356/386 original, 321/382 neutral-retained and
296/382 pure-neutral. Presentation-only changes in a72d214 do not alter the
qualified recognition implementation.

## Formulas in the completed benchmark

For each presentation, elapsed time `T = perf_counter(end) - perf_counter(start)`.
The headline comparison uses `sum(candidate T) / sum(control T)` for the same
1,150 presentations. The retained headline `seconds` field rounds each duration
to four decimal places; full-precision wall counters are also retained. The
speed reduction against the current reference is
`100 * (1 - sum(candidate T) / sum(reference T))`. It is not an average of the
individual PDF ratios. The controller's total duration is separate and also
includes startup, observation and bookkeeping overhead.

Application CPU work is `C = parent CPU delta + sum(completed child CPU)`, where
each CPU counter is kernel plus user time. Direct OCR children are accounted
from their retained Windows handles after communication completes, rather than
inferred from occasional process-tree samples. Missing counters remain reported.
Parallel child CPU seconds add and can exceed elapsed wall seconds. The 1.56%
CPU increase therefore describes additional measured work, not slower latency;
it alone does not prove more useful evidence was acquired. This qualification
retained the same acquisition plans and OCR input/output evidence.

With `N` logical machine CPUs, equivalent busy cores are `C / T` and application
machine-capacity usage is `100 * C / (T * N)`. Aggregate usage is
`100 * sum(C) / sum(T * N)`, rather than a simple mean of call percentages.
Windows system busy CPU is `delta(kernel including idle + user) - delta(idle)`.
System busy percentage is `100 * busy / delta(kernel including idle + user)`.
Estimated external CPU work is `busy - C`; its machine-capacity percentage is
`100 * (busy - C) / (T * N)`. Endpoint counters are not sampled atomically, so
small negative residuals remain visible rather than silently clamped.

AC/battery and saver state, application RSS/private memory, available system
memory, system CPU activity, process counts, periodic process inventories,
power scheme/mode and service counts provide context. They are not multiplied
into a synthetic adjusted time. Nominal clock reports do not measure thermal
throttling. Endpoint/lifetime memory observations are not complete peak-memory
accounting for every short-lived child. The grouped bootstrap resamples PDF IDs
with their filename presentations together; it does not establish independent
publisher-family generalization or machine/day repeatability.

## Completed concurrency comparison

Six ordinary-neutral PDFs were acquired freshly at each concurrency setting.
All settings produced the same author results, expert traces and OCR input/output
hashes, with 79 OCR readings per setting. Source and input hashes were unchanged.

| OCR workers | Summed elapsed seconds | Parent CPU seconds | OCR CPU seconds |
| ---: | ---: | ---: | ---: |
| 1 | 50.0702 | 13.3281 | 33.4375 |
| 2 | 36.6035 | 14.1406 | 36.2812 |
| 4 | 28.8617 | 14.5313 | 40.0313 |
| 8 | 27.0536 | 14.8125 | 46.0469 |

Eight had the lowest observed sum. The existing automatic cap is retained.
These 24 calls are a bounded comparison, not proof of an optimum for every
machine, document or simultaneous batch. The comparison was completed after
the full benchmark and has not been needlessly rerun for this review.

## Reporting and diagnostic fixes

`python -m benchmarks.ocr_plots observations.json --output-stem output/chart`
renders completed observations without running recognition. Matplotlib is
optional for the application and must exist in the explicitly selected analysis
interpreter. The project's existing analysis environment has Matplotlib 3.11.0;
the bundled spreadsheet-authoring interpreter does not. No package or shared
runtime was installed or changed during this repair.

The renderer derives workload labels from the actual retained groups, includes
low outliers when present, accepts zero-OCR workloads without a singular axis,
rejects nonfinite durations and inconsistent group/work counts, and separates
footer text from axis labels. Source/version information is retained; reference
observations are not silently relabelled as new-candidate measurements.

The environment observer takes one worker-set snapshot per sample. Registering
a worker while reading memory therefore cannot terminate its loop with a
set-size-change exception or apply inconsistent ownership views within a sample.

Focused regressions cover sparse groups, low outliers, invalid durations,
population/work reconciliation, zero-OCR PNG/SVG rendering and worker registration
during observation, plus adjacent existing resource/inventory tests. Recognition,
OCR evidence acquisition, expert priority and effort policy are unchanged in
this reporting/diagnostic stage. It requires no application-server restart.
