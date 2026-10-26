# Setup, native parsing, upload inventory and finalization

This extends `ab4c31a` without editing author experts, role priorities, filename
rules, OCR configuration, Unstructured extraction or exact vector requirements.

## Changes

1. Confirm-time ETA now aggregates numeric facts from the current, exact-version
   preflight manifest. It no longer reopens those PDFs for another 24-page sample.
   A few character counts per source retain exact aggregate density statistics;
   the batch history does not duplicate the combined sample array. Incomplete or
   stale facts fall back to the existing profiler. Required all-page coverage is
   retained.
2. Plain text and positioned native lines use one full-page TextPage with matching
   flags. The TextPage never leaves its original Page/Document lifetime. Only
   Python measurements transfer into later layout interpretation, consumed once
   after content, file-version and geometry checks. Dirty/changed handles and
   incompatible geometry fall back to fresh reads. Drawing, footnote, running
   furniture and OCR decisions still execute separately in their original order.
   There is no persistent or cross-request document cache.
3. Pre-attachment inventory uses fresh directory-entry metadata rather than a
   separate Path stat for every existing JSON entry. The full sorted path set,
   including nested paths and platform-specific extension matching, is retained.
   Links remain excluded. Live read-only comparison returned the same 22,666
   paths: 5.003 versus 0.067 seconds. This is inventory timing, not embedding
   throughput.
4. Private successful-run retention checks existing canonical segment bytes
   before staging a temporary copy. Missing bytes still stage normally, collision
   guards remain, and public exports/provenance/recovery evidence are retained.
   It avoids writing files merely to read them back and discover they already
   exist.

## Evidence and limits

Two local full preparations (6 and 33 pages) matched the reference on every TXT
content hash, selected manifests, complete author inference excluding collection
duration, and selected metadata fields. The Field excerpt's native decode/layout
was 52.672 versus 23.051 seconds; whole preparation was 123.150 versus 99.214
seconds. These sequential observations are not a controlled whole-corpus ratio.

The first real Automatic callback processed both complete sources into a new test
workspace using factory defaults and no author overrides. All 39 records were
uploaded, freshly embedded (zero cache eligibility/reuse), and exactly confirmed.
Preflight coverage took 23.6 seconds, the subsequent setup-to-first-worker gap
0.64 seconds, and finalization 0.57 seconds. The callback test excludes browser
selection/preview. Its assistant history was isolated; it does not reproduce a
large production history or a large ingestion's finalization cost.

Final qualification passed Ruff, core Pyright, 3,528 offline tests and 28 subtests
(two skipped, 43 deselected). All native text, transformed pages, layout evidence
and OCR-call counts matched on the 31 PDFs / 2,951 pages. TextPage construction
fell from 5,921 to 2,970; observed native extraction/layout time was 33.667 versus
30.981 seconds. This comparison does not run Unstructured, full author inference
or upload for that set. Only measured `targeted_ocr_seconds` and
`subprocess_seconds` are excluded from evidence equality.

The final integrated callback again uploaded, freshly embedded and exactly
confirmed 39 records. It created zero temporary retained-segment copies. Total
callback time was 140.336 seconds versus 140.121 in the earlier candidate test:
these small runs do not establish an additional end-to-end speed gain. The earlier
candidate already contained native parsing/ETA improvements. The final run also
records power, memory, system CPU activity and parent-only CPU separately, without
inventing a load-corrected duration or claiming to measure child CPU with the
parent counter.

A separate isolated retention replay used the actual 5,420 archived prepared
segments from all 42 October sources. Both versions produced the same complete
export-name/content digest. Temporary segment writes fell from 5,420 to zero;
retention itself took 18.255 versus 16.578 seconds. Hardlinks supplied immutable
test inputs. This is a retention-only comparison, excluding PDF processing, API
calls, workspace changes, public publication and test-fixture setup. It does not
explain or eliminate the entire historical 91.6-second post-batch window.

The 1,150-presentation metadata benchmark was not rerun for these changes. Its
previous scores remain reference values, not newly measured results. Neither
new author-accuracy gains nor a new full-ingestion 1.2x qualification is claimed.
No new progress/logging stream is introduced. Test command logs are tail-bounded;
phase evidence uses scalar summaries and hashes, not additional PDF/text dumps.
