# Exact native measurements inside author-field review

Repeated crop interpretations used fresh evidence contexts, causing the same
native pages to be measured repeatedly. The field-review invocation now shares
only exact native measurements, while every acquisition plan, genre/profile
interpretation, role assessment and expert verdict still executes independently.

## Boundaries

- Scope is one call to `review_author_field_map`; no data survives its exit.
- Keys bind the source SHA256 and stable file identity, newly encountered
  document handles, page/xref, rotation, media/crop geometry, glyph-height setting,
  operation and exact arguments. Dirty, encrypted, changed or unverifiable inputs
  bypass sharing. Exceptions are never retained as successful reads.
- Consumers receive independent mutable values. Positioned native atoms, native
  text/dictionary views, word reads, overlay text traces and image/drawing facts
  can be shared. Field proposals, acquisition plans, evidence bundles, OCR,
  priorities and accepted authors cannot.
- Different crops, page segmentation modes, OCR retries, text layouts and genre
  interpretations remain distinct. There is no persistent/cross-request cache,
  GROBID integration, skipped expert or altered success threshold.
- The later field review remains diagnostic-only; this change does not promote
  its results into the final author decision.

## Qualification

The complete offline suite passed 3,500 tests, one skipped, 43 deselected and
28 subtests. New boundary tests cover reopened exact source bytes, independent
mutable consumers, page/option/document separation, dirty documents, source
mutation, exception retry and request-scope disposal.

A fresh complete metadata run tested all 1,150 filename presentations across
382 distinct source byte streams. It retained the qualified complete-roster
scores: 356/386 original presentations, 322/382 neutral presentations retaining
internal metadata, and 297/382 pure-neutral presentations. There were no changed
authors, correct-to-incorrect transitions or processing exceptions. Full saved
inferences, acquisition plans, expert attempts, OCR evidence text/geometry and
decisions matched the previous Thorough results after removing only the
collection-duration measurement. All scheduled experts completed; input/source
hashes remained fixed.

Four separate paired full field reviews retained equal results after excluding
timer values and temporary OCR output paths. The selected native read families
made 812 logical requests, with 186 physical reads and 626 exact reuses. Each of
two Field chapters retained 240 logical requests but required only 50 physical
reads. Their observed review latency improved substantially; Cramer did not.
Across the four cases, observed time was 241.9 versus 113.5 seconds. This is a
diagnostic sample, not a qualified whole-application speed ratio.

A stricter two-PDF direct-review comparison captured every nested Tesseract
subprocess. Both PDFs kept three distinct OCR calls each. Actual input-image
hashes, executable/options, output hashes and complete review decisions agreed.
The full 1,150-call metadata worker did not independently capture subprocess
pixel hashes; its complete saved semantic evidence was compared separately.

Coverage is fresh metadata inference, offline regressions and sampled full
shadow review, not new full-document ingestion, output equivalence for every
PDF, or renewed manual validation of every expected corpus author. The final
production files have identical parsed ASTs to the frozen qualified source;
only a lint annotation preserves the existing local import order. Test-only
formatting and unused-import cleanup do not change production behavior.

Post-integration lint and whitespace checks passed, along with 50 targeted
regressions. One direct review through the canonical production module matched
the qualified candidate and retained its OCR call. No GUI workflow or full
ingestion was exercised by that direct check.

## Separate findings left unchanged

Historical timing analysis locates much of a roughly 90-second setup wait in
progress profiling of up to 24 pages per PDF. A 42-PDF diagnostic sampled 859
pages; three Field chapters dominated native text/drawing work. Progress-profile
sampling is not changed in this release.

Vector observations remained two per upload group in all three historical runs.
October added stronger physical vector-ID/provenance checks. No vector sampling,
weaker verification, embedding policy or upload behavior was introduced.
