# Context-dependent author evidence experiment

Different specialists may use the same visual, native text and OCR cues. Their
scores describe different hypotheses and are not interchangeable probabilities.
The experiment combines their interpretations using situation-dependent decision
weights and retains the evidence responsible for those contributions.

| Situation | Priority |
| --- | --- |
| Footnote/footer/copyright region plus a linked author credit | Preserve the author; these region roles can coexist with authorship. |
| Explicit translation, foreword or institutional staff credit, with only a weak name/filename candidate | The competing role has enough weight to defer that candidate. |
| Separate title-linked authorship and another occurrence as translator | Authorship retains priority; the role has less influence in this situation. |
| Editor list for a whole volume | Treat as valid creator metadata under the volume policy. Do not promote parent editors to chapter authors. |
| A person-shaped field near a title, without a verified credit boundary | Low influence; it cannot independently select an author. |
| Unresolved opening with a catalog record or title/name/introduction structure | Retry the existing selector once with a visible-title hypothesis. |
| Equally plausible competing work credits | Preserve an established supported result or abstain. |

These are heuristic rules, not trained region models or calibrated percentages.
The current prototype covers overlapping footer, footnote and copyright-region
hypotheses and explicit competing-role credits. A reference-to-another-work role
has a decision policy but does not yet have a new production region classifier.
The existing field mapper continues to offer additional diagnostic roles.

The scope is bounded: native field checks inspect at most four opening pages;
the title retry runs once; the native article fallback visits a second page only
when the first page is a sparse wrapper. No extra OCR call is introduced. There
is no network resolution, GROBID, expected-answer input, author lookup table or
PDF-hash lookup. Existing difficult cases remain deferred.

Qualification uses the frozen 41 and 172 populations, 196 labeled course views
in both filename modes, and the deduplicated 257-document neutral union. Expected
labels and complete-name scoring remain unchanged. The earlier 20-paper holdout
is now a regression set; reusing it does not create another blind evaluation.

A release requires 193/196 ordinary course results, the preserved 39/41 and
166/172 floors, neutral-filename improvement, and passing relevant tests. A
candidate below the ordinary-course threshold must remain experimental.
