# Embedded author proposals: later-role review

Embedded Author/creator and bibliography-shaped Keywords values remain proposals.
The shared adjudicator now supplies all already acquired responsibility claims
to the embedded-credit expert before final selection. A matching later translator,
editor, designer, foreword, series-editor or other secondary-role claim withholds
the weaker embedded authorship claim unless a separately scoped primary-author
act supports the person's dual role. Cited/reviewed-work roles do not veto an
unrelated selected-work author. Other experts' independently established claims
remain available to the adjudicator.

This adds no acquisition, changes no native/OCR page plan, and introduces no
reusable PDF, author or OCR cache. The affiliation grammar is compiled once with
equivalent matching semantics; affiliation checks are skipped for ineligible
field-group kinds. GROBID remains disabled.

## Completed private corpus experiment

The frozen candidate was evaluated over all 1,150 presentations: 386 original
filename cases (382 distinct PDFs plus four intentional aliases), 382 neutral
filename copies retaining embedded metadata, and 382 pure-neutral copies with
identifying embedded metadata stripped. Each inference freshly acquired evidence.
Baseline/candidate order alternated; mains power and unchanged frozen source
hashes were checked. No simultaneous PDF/test workload ran during measurement.

| Filename condition | Reference-roster matches | Regressions from candidate22 |
| --- | ---: | ---: |
| Original | 351/386 | 0 |
| Neutral, embedded metadata retained | 317/382 | 0 |
| Pure neutral | 292/382 | 0 |

All 1,150 calls completed without processing/acquisition/expert failures. Native,
opening and OCR plans match candidate22 for every presentation. Relative to the
previous production reference this preserves three original-name and seven
neutral-name recoveries; the later-role/performance edits themselves do not alter
the candidate22 author strings.

After completion, runtime was evaluated once. Candidate total: 3,799.4576 seconds.
Fresh paired baseline: 2,727.2093 seconds (1.39317x); older retained baseline:
2,122.1814 seconds (1.79035x). The user's revised cap is 1.73x. The fresh paired
comparison passes; the retained historical comparison fails. These are different
comparisons, not interchangeable timings. The contemporaneous fresh paired
baseline governs the 1.73x gate: it was collected over the same presentations,
under the same alternating schedule and observed power conditions. The retained
older timing is historical context. Requiring it as a second release gate was
an additional experiment-controller condition, not a user requirement.

The complete candidate therefore passes the accepted runtime gate and all
recorded accuracy/evidence gates. No further speed experiment was run to obtain
that decision. Production integration checks are separate from this inference
benchmark; these timings do not describe full PDF preparation or embedding.

The private artifacts retain the original controller verdict (which used the old
1.7x cap and both comparisons), plus the final updated-cap audit. They are under
the research project's `outputs/recognition-preview-20261020` directory.

## Validation and limits

The complete offline suite on the integrated production source: 3,323 passed, one
skipped, 35 deselected, 28 subtests passed. Targeted controls cover later native
translation, shared later OCR roles, dual roles, unrelated-work roles, actual
coordinator reconciliation, and affiliation-grammar equivalence. The browser
button test is separately opt-in and is not part of this offline count.

These are reference-roster matches on the existing development population. The
run is initial-page metadata inference, not full PDF preparation or embedding,
and it does not establish independent-corpus generalization or a new uniform
manual review of every source. The simultaneous UI changes require their own
actual-server qualification before deployment can be called complete.
