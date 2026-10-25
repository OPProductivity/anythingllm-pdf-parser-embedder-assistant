# Lexical parsing, geometry and responsibility review, 25 October 2026

The complete metadata benchmark passed recognition and evidence qualification.
The candidate has a modest elapsed-time improvement in this matched run; the
1.2x aspirational target remains unmet. Final integration and live two-port
qualification are recorded below when complete.

## Corpus and comparison

The retained suite contains 382 distinct original PDF byte hashes and 1,150
filename presentations: 386 original presentations, including four deliberate
aliases; 382 renamed, byte-identical neutral presentations retaining internal
metadata; and 382 pure-neutral copies with identifying internal metadata removed.
These are not 1,150 independent PDFs or works. Every version makes 1,150 final
author decisions. The three-version experiment therefore makes 3,450 fresh
metadata inferences, rather than 3,450 GUI selections or full-document uploads.

The original runtime control is `baseline-source-3bc-matched14`; the fresh
current-main reference is `903ad6d`. Inputs and source snapshots were frozen.
The controller kept author labels outside workers, cycled all six version orders
across the suite, and checked mains power and battery-saver status before and
after every call. All input hashes passed the after-run check.

| Filename condition | Fresh current main | Candidate |
| --- | ---: | ---: |
| Original | 352/386 (91.2%) | 355/386 (92.0%) |
| Neutral, metadata retained | 318/382 (83.2%) | 320/382 (83.8%) |
| Pure-neutral | 293/382 (76.7%) | 295/382 (77.2%) |
| All presentations | 963/1,150 | 970/1,150 |

No previously successful presentation regressed, either against fresh main or
its retained prior results. The original-name 96% target requires at least
371/386 complete matches and remains unmet. Complete-roster reference matches
are not a new manual validation of every expected author in the corpus.

## Computation changes

- Reuse pure book-name parsing inside one inference's existing lexical scope.
  Fresh lists remain independent; entries are discarded after the inference.
- Compute identical-membership native line geometry once while preserving
  separate proposal IDs, interpretations and mutable lists.
- Restrict baseline search to geometrically possible earlier rows while keeping
  the original exact predicate and first-eligible-row order.
- Avoid repeated regex starts inside a whitespace suffix without changing its
  author/editor suffix semantics.

There is no persistent PDF, OCR or author-result reuse, GROBID integration,
narrower acquisition plan, skipped expert or change to OCR pixels, languages,
segmentation modes, retry policies or concurrency limits. The broader laboratory
acquisition mode remains available. Computation reuse does not merge experts'
interpretations or reduce the weight of repeated contextual evidence.

The geometry comparison retained complete output equality on 1,000 generated
layouts and 40 real native page maps under fixed test queries. The regex suffix
check compares the old and new behavior across 10,000 whitespace/word combinations.

## Publishing responsibility repairs

The manually inspected sample comprises 23 PDFs, using complete first-five-page
overviews, native text and selected full-page detail views; one PDF has only four
pages. Existing full-roster labels remain separate from what this round directly
verified. No expected labels were changed to improve a score.

The eleven changed decisions belong to five reviewed PDFs:

- An IBM report's interleaved biography columns defeated an immediate-next-line
  credential check. A bounded local window now recovers its three printed
  authors, while excluding credential phrases such as Distinguished Engineer
  from the person roster. Collective current-report responsibility is still
  required; a list of interviewed experts alone is insufficient.
- A company overview's ownership narrative includes modifiers between `our`
  and `platform`. Existing company heading, possessive and operational-profile
  requirements remain in place; an embedded compiler field is not promoted.
- A scanned chapter already contains the complete author in acquired OCR.
  A local heading/byline/body relation plus a curated surname can survive an
  unrelated scanner title. This repair improves only its original-name case.
- Cover and contents evidence establish that a local byline belongs to a
  foreword within a larger report. The candidate abstains instead of attributing
  the complete report to that contributor; independent whole-work proof and
  standalone forewords remain protected.
- A parent-book title was mistaken for a running author head. A structured
  author/chapter/parent-title filename now keeps that title out of the weak
  author proposal. A surname-only fallback remains partial and is not counted
  as a complete-name success.

The proposed personal-blog filename promotion was rejected after it conflicted
with the retained conservative masthead-only fixture. The hosting-versus-post
authorship policy and expected labels were preserved. Further label/policy review
is needed; this is not proof that the real post has no author.

Speed-only computation reuse does not change expert priorities. The publishing
repairs change conditional eligibility and work scope, as recorded separately
in the new workbook review sheets; existing numerical priority values remain.

## Elapsed time and limits

| Version | Summed metadata seconds | Ratio to original control |
| --- | ---: | ---: |
| Original control | 2,216.932 | 1.000x |
| Unchanged current main | 2,945.239 | 1.329x |
| Candidate | 2,913.493 | 1.314x |

The fresh candidate/current-main ratio is 0.98922: 1.08% less elapsed time in
this run. The PDF-group bootstrap range is 0.98062–0.99757. It describes 3,000
resamples of this document mix, keeping filename conditions and aliases together;
it does not establish machine or day-to-day repeatability. Multiple versions of
a work may still share publishing conventions across review IDs.

The timer uses `perf_counter`, not Python CPU time. It covers metadata sampling,
identity resolution, acquisition and inference, including OCR waits. It excludes
UI queueing, full-document processing, uploads, embeddings and retrieval. CPU time
for Python plus OCR subprocesses, temperature and CPU frequency were not recorded.
No concurrent PDF rendering, test suite, preview or ingestion work ran during
measurement; bounded read-only operational queries were made while answering
questions. Windows filesystem caches were not flushed.

The previous run's unchanged-main time and original-control time were lower.
Those historical absolute timings must not be substituted for this fresh paired
comparison. Future measurement improvements should add child-process CPU work,
phase timings and environment observations, and balance order within filename
conditions as well as across the complete suite. No extra timing run was started
to obtain a favorable ratio.

## Integration and two-port operation

The frozen recognition candidate passed 3,349 offline tests, one skipped test,
43 deselections and 28 passing subtests. The full metadata run had no processing
exceptions, incomplete scheduled acquisitions, changed acquisition plans or
input-file modifications. All eleven changed decisions were manually reviewed.

Browser session cookies previously shared one name across localhost ports.
A same-cookie-jar regression reproduced the 7860 logout after authenticating
7861. The separate browser fix namespaces cookies by the trusted ASGI listener
port, retaining per-server token validation and origin restrictions. It is
outside the timed recognition path and is qualified separately.

Final integrated tests, real server checks and snapshot deployment are pending.
Local experiment records and expanded workbook remain under the Codex project's
`outputs/metadata-lexical-geometry-20261025/` directory. The previous expanded
workbook and all its historical sheets remain intact.
