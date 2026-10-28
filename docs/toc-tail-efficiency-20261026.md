# Same-corpus OCR completion and native-copy efficiency

This extends b8d27d3. It changes no author rules, expert priorities, acquisition regions, OCR pixels/models/options/retries, concurrency limits, output policy or exact-vector requirements. No persistent document/OCR/result cache or GROBID is introduced.

## Diagnosis

The preceding run increased Q3 by 1.425 seconds (3.34%), p90 by 2.774 seconds (4.06%) and p95 by 1.447 seconds (1.61%), while its maximum fell 10.139 seconds. Q3 was halfway between Hughes and Susman, p90 was the De Grazia introduction backup, and p95 was halfway between Adorno/Horkheimer and the Consumer-Citizen backup. With 31 files these are one- or two-file thresholds, not independent repeated measurements. Retained reports and instrumented call counts matched; there was no evidence of added investigative work. Exact environmental causes cannot be identified from aggregate resource counters.

## Narrow changes

- A finished OCR region now posts a completion notice to the existing owner request queue. The owner no longer has to wait for its next 20 ms poll when no raster request arrives. Source rendering remains on the owner thread; every region/retry still runs; results retain plan order and cancellation/failure handling.
- Native immutable-tuple inspection tests scalar children directly. Mutable-container copying, alias/cycle handling, extension fallback and all source/geometry guards are unchanged.

An alternating component probe using real native values found 35.4% shorter copy time. A separate synthetic short-region probe demonstrates removal of polling latency; its percentage is not a full-PDF performance result. An OCR-thread-limit probe was inconclusive, so no thread-limit change was adopted.

## Qualification and preservation

Ruff and core Pyright passed. The complete offline code suite passed 3,541 tests and 28 subtests; two tests skipped, 43 deselected. This is separate from the 1,150-presentation recognition benchmark, which was not run at the user's request.

All 31 PDFs / 1,683 pages were processed again using factory defaults, original filenames, full sources, blank overrides and the same order. All 1,723 exported TXT names/bytes, 1,659 uploaded content/citation records, selected metadata, initial inference, complete retained field reviews, embedding configuration/policy and selected-output OCR flags match. Timing fields and fresh record IDs/ingestion timestamps/temporary URLs are excluded. This is preservation of existing decisions, not renewed manual verification that every author is correct.

All 1,659 records were freshly embedded with zero existing target records and zero cache eligibility/reuse. All 31 sources reached exact_vectors_proven without processing errors. The prior workspace, its namespace, SQL vector links, unshared records and associated cache entries were removed first; unrelated workspace counts and original PDFs were preserved.

## Per-PDF preparation distribution

Percentiles use R7 linear interpolation across the same 31 worker durations, including worker startup.

| Metric | Previous seconds | New seconds | Change |
|---|---:|---:|---:|
| Mean | 33.054 | 32.709 | -1.04% |
| Median | 27.441 | 28.210 | +2.80% |
| Q1 | 9.396 | 9.117 | -2.96% |
| Q3 | 44.036 | 40.738 | -7.49% |
| p90 | 71.138 | 65.070 | -8.53% |
| p95 | 91.039 | 91.921 | +0.97% |
| Maximum | 114.630 | 130.282 | +13.65% |

Negative change means shorter; positive means longer. These observations do not establish a uniform tail improvement.

## Whole-run phases

| Phase | Previous seconds | New seconds |
|---|---:|---:|
| metadata_preview_first_five | 8.355 | 9.157 |
| selection_advisory_eta | 0.156 | 0.165 |
| workspace_creation | 0.340 | 0.324 |
| confirmation_preflight | 6.617 | 7.853 |
| setup_to_first_worker | 0.754 | 0.741 |
| local_preparation | 1032.278 | 1021.799 |
| upload_embedding_vector_confirmation | 347.733 | 845.167 |
| finalization | 11.711 | 12.647 |
| Total | 1407.945 | 1897.853 |

Upload/embedding/confirmation code is unchanged in this release. Sequential embedding-group windows grew from 229.568 to 727.942 seconds, while attachment/setup/other residual work stayed essentially flat (118.165 to 117.225 seconds). Actual vector-store observation grew only from 15.755 to 20.961 seconds, nested inside those windows. The additional whole-run time is concentrated in backend group handling/receipt/reconciliation windows, not local metadata work or a large increase in actual vector scanning. These timers do not isolate provider-only compute or establish the precise service/environment cause. Inclusive source-stage and self/CPU detail, individual slower cases, all numerical comparisons and one percentile graph are retained in outputs/toc-tail-efficiency-20261026 in the Codex project.

## Remaining outliers

The original Consumer-Citizen scan took 130.282 versus 114.630 seconds. Its native candidate grew from 57.889 to 66.785 seconds and field review from 44.908 to 52.471 seconds; initial inference was slightly faster. Native layout itself is outside the two edited mechanisms. The initial and retained field-review reports and every instrumented call count match. Adorno/Horkheimer also increased, primarily in unchanged Unstructured decoding and field-review elapsed time. This supports a substantial variability contribution but does not measure how much is thermal, scheduling, I/O, OCR-process variation or a scheduling interaction. No favorable-only rerun was substituted.

No extra full-corpus recognition percentage or general-purpose 1.2x speed qualification is claimed. The publishing-knowledge workbook is unchanged. The new workspace is retained: toc-iii-tail-comparison-60799726.

## Read-only timing follow-up — 28 October 2026

The retained backend event journals make the 1,659-record upload slowdown more specific. The 65 successful embedding SDK calls had a median of 1.825 seconds in the earlier run and 4.489 seconds in the slower run. None exceeded 10 seconds earlier; 17 exceeded 10 seconds and nine exceeded 20 seconds later. Those slow calls appeared across roughly 12 minutes and included batches of 9, 10, 14, 17, 18 and 36 chunks, so the delay was neither confined to one source nor explained by batch size alone. Several calls took about 30–35 seconds. The adapter's configured per-call timeout was 600 seconds, and it logged one successful adapter attempt for each call. The retained events do not expose retries inside the Desktop SDK or upstream request IDs; the near-30-second cluster cannot be assigned to a specific provider, network or retry mechanism. Aggregate system CPU and available-memory readings were similar across the two full runs and do not show an obvious whole-machine saturation event. The smaller subsequent 31-record upload-only crossover did not reproduce the large-run delay.

The same 45-page original *Consumer-Citizen* PDF, at one source SHA-256, took 124.769 seconds in the first qualified full Automatic run, 114.630 seconds in the `b8d27d3` comparison and 130.282 seconds in the `b8e930a` repeat. A later direct local `prepare_pdf` invocation on the current production source took 151.007 seconds. That fourth invocation used a different wrapper and was not a controlled same-environment worker repeat, so it must not be interpreted as a measured cross-version regression. Its parsed TXT bytes match the `b8d27d3` run exactly (SHA-256 `726950c7369993ac4b94b308b417b48de955b7728294b497cb090c3d693ab318`). Both runs recorded 38 native layout decisions and the same three author-field OCR compositions; the field review left final metadata unchanged. The current native-layout and field-review code is unchanged from the selected baseline, and its new optional page-preserve ceiling was zero for this file. The current 151-second observation is high relative to these three full-run receipts, but the repeated expensive stages and identical output support a timing-sensitive hard-PDF tail rather than demonstrated extra extraction work from the recent workflow fixes. A comparable repeated Automatic-worker run would be needed to quantify any current regression.
