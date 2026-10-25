# Slowest-PDF diagnostic without exclusions

This is a counterfactual analysis of the completed 1,150-presentation matched
run, grouped into 382 distinct PDF byte streams. No PDFs, author requirements,
acquisition plans or recognition rules were removed or relaxed. Related editions
and excerpts remain related works, not independent publishing families.

The candidate used 2,913.4932 seconds against 2,216.932 control seconds:
1.3142005x. Reaching 1.15x on this unchanged full baseline requires saving
364.0214 seconds, or 12.49% of candidate time. Mean time per presentation was
2.533 seconds and median time was 1.715 seconds.

## Ten or twenty hypothetical omissions

The slowest ten distinct PDFs total 381.6361 candidate seconds, 13.10% of time.
Removing those PDFs from both candidate and baseline yields 1.205905x, not 1.15x.
The mathematically optimal ten-PDF omission set yields 1.188494x. Therefore no
ten-PDF removal reaches 1.15x in these observations. The optimal set for this
ratio is P078, P140, P179, P313, P084, P105, P244, P155, P054 and P245.

For a proposed target r, select the ten greatest values of C_i - r B_i, where
C_i and B_i are candidate and baseline time for the same PDF across its
presentations. Binary search on the remaining ratio gives the optimal fixed-size
set. Dropping a slow document that was equally slow in the baseline may worsen
the ratio. These are diagnostic calculations, not a new qualification result.

The slowest twenty total 614.9273 seconds, 21.11% of candidate time. Their omission
would yield 1.147701x while reducing coverage. Keeping all PDFs and obtaining the
required saving solely within those twenty would require reducing their cost
by 59.20%. Doing so solely within the ten slowest requires 95.38%, effectively
eliminating almost all their processing. Neither is a demonstrated optimization.

## Ten slowest by total candidate cost

Times sum original, ordinary-neutral and pure-neutral presentations of each PDF.
They do not mean one PDF takes the summed time in ordinary use. OCR attempts
below are for ordinary-neutral; planned regions may receive additional readings.

| ID | Source | Total seconds | Neutral seconds | Neutral OCR attempts |
| --- | --- | ---: | ---: | ---: |
| P078 | Prizing Children's Literature, searchable version | 49.64 | 18.57 | 16 |
| P049 | Handbook of Latinos and Education | 49.33 | 16.38 | 9 |
| P117 | Salesforce State of Sales, fifth edition | 45.67 | 15.45 | 43 |
| P179 | From Silver to Cocaine | 44.52 | 14.70 | 19 |
| P140 | Latino terminology: conceptual bases | 41.45 | 13.80 | 11 |
| P313 | Garncarz, Art and Industry | 40.19 | 13.74 | 14 |
| P084 | Prizing Children's Literature, other PDF | 35.13 | 10.91 | 16 |
| P155 | Open Veins of Latin America | 25.42 | 8.53 | 19 |
| P054 | Making Pan-Latino Ethnicity | 25.15 | 8.67 | 8 |
| P105 | Reading the Latino in Latino Studies | 25.13 | 8.23 | 7 |

The next ten are P244, P030, P248, P240, P245, P098, P226, P198, P318 and P171.
All twenty retain their official scores. They include successful and unresolved
sources; none was newly declared exceptional. P078/P084 and P244/P245 are
related versions and must not be counted as separate publisher families.

## Five fresh diagnostic profiles

Only P078, P049, P117, P030 and P171 were profiled again, each ordinary-neutral.
All five preserved the exact author output, native/OCR plans and OCR attempts.
These instrumented elapsed timings do not replace the official speed run.
Parent CPU was measured with process_time; Windows GetProcessTimes measured
completed Tesseract child CPU. Concurrent CPU can exceed wall time. Nested spans,
worker durations and multithread call-profile totals overlap and are not additive.

- P078: 13 raster regions, 16 Tesseract processes, 4.38 seconds caller-thread
  raster preparation and 13.44 OCR-child CPU seconds in the diagnostic call.
  Hidden text overlays coexist with page images and local byline crops.
- P049: seven raster regions, nine Tesseract processes, 5.83 seconds raster
  preparation and 13.89 OCR-child CPU seconds. This is a 708-page book, but the
  plan samples seven native pages; it is not processing all 708 pages.
- P117: 26 image regions, 43 Tesseract processes and 20.89 OCR-child CPU seconds.
  Seventeen empty/low-quality orientation rechecks contribute. The original
  control was already slightly slower, so this expensive source is not a strong
  target for improving the candidate/control ratio by exclusion.
- P030: five regions, two blank assessments and five actual Tesseract processes.
  Context-priority, text extraction and native field construction also contribute
  outside collection. Few OCR processes do not imply negligible interpretation.
- P171: 34 native pages, two image regions and three Tesseract processes. The
  profile records 209 get_text calls and 32 map_page calls. Native text/geometry
  construction matters substantially more than the OCR count suggests.

## Diagnosed continuation defect and next evidence-preserving experiments

Netflix pages 5, 12, 15, 18, 30 and 32 begin with a Table of Contents navigation
header above ordinary report prose. Literal comparison with
evidence_collection._apparatus_continuation shows that its first-three-lines
heading match treats this as continuing apparatus. It extends the opening plan
to page 32, in addition to two closing pages. This is a diagnosed planning defect;
no correction or reduction in evidence was implemented in this exercise.

Future trials should distinguish a repeated navigation header from the actual
contents using position, repetition, linked navigation and body structure.
Other candidates are deriving text and fields from one lossless native page
representation within the same inference, reducing repeated raster work, and a
bounded rolling OCR queue rather than waiting for every task in an eight-region
batch. Each must preserve all required readings, pixels, retries and decision
order and pass the complete suite before integration. No persistent evidence or
recognition-result cache was added.
