# Metadata recognition qualification, round 05

This release reduces repeated title/affiliation parsing and native PDF
extraction within one inference. A rolling OCR scheduler fills available
worker slots while retaining the same regions, pixels, retries and output
order. PDF rendering remains on the caller thread. No PDF/OCR result cache,
new dependency or GROBID integration is introduced.

A separate organizational responsibility rule joins a legal company credit,
a matching recurring domain and unquoted publishing narrative. Copyright
ownership alone is insufficient, and explicit individual authors retain
precedence. One manufacturing brochure improves in the development corpus;
this is not evidence of broad independent generalization.

## Complete matched metadata benchmark

The comparison uses 382 distinct PDF byte streams: 386 original-filename
presentations (including four aliases), 382 renamed presentations retaining
embedded metadata, and 382 pure-neutral presentations with identifying
embedded metadata removed. Three source versions process each presentation
fresh, for 3,450 metadata-inference calls. Expected author labels are unchanged.

| Condition | Main reference | Candidate |
| --- | ---: | ---: |
| Original filename | 355/386 | 356/386 |
| Neutral filename, metadata retained | 320/382 | 321/382 |
| Pure neutral | 295/382 | 296/382 |

The only changed author is the same brochure in all three presentations.
There are zero correct-to-incorrect transitions, processing exceptions,
acquisition-plan differences or OCR input/output hash-multiset differences
against the main reference. Input, recognition-source and measurement-helper
hashes remain unchanged throughout the run.

Observed elapsed time is 4.44% lower than the matched main reference and
1.21498 times the original control. The matched main reference itself measures
1.27147 times that control in this run. Parent Python CPU time falls 2.76%,
while total Python-plus-OCR CPU time rises 1.56%. These describe observed
latency and CPU work separately; no synthetic contention-corrected time is
reported. Power/load telemetry is retained, including 274 calls outside the
preferred mains/no-saver condition. A single run does not establish machine
or day repeatability, and the 1.2 goal is not claimed achieved.

The complete offline suite passes: 3,374 tests, one skipped, 43 deselected,
and 28 subtests. A separate 24-call comparison covers six PDFs with each of
one, two, four and eight OCR workers. Author results, expert traces and OCR
inputs/outputs agree. Eight workers has the lowest summed observed latency
in that sample; the existing automatic worker limit is retained.

Coverage is metadata inference and offline regressions, not full PDF-to-text
output equivalence, live uploads or embeddings. Complete-roster scores include
three correctly empty-author references per condition. Partial names and
organizational aliases remain distinct from exact roster matches.

The experimental effort slider, early stopping, navigation planning changes
and OCR-coordinate interpretation trials are excluded from this release.
The five-loss allowance applies only to a future qualified Thorough policy;
it is not used to relax this release's zero-regression result.
