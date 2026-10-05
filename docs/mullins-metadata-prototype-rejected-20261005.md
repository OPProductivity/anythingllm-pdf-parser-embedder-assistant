# Mullins metadata prototype: rejected

User authorized a cautious isolated prototype with before/after checks on other
PDFs, not production deployment. Baseline source `28448bc`; prototype/evidence
`C:/Users/Ninkear/.codex/tmp/p03-proto`. Independent coordinator evidence:
`C:/Users/Ninkear/.codex/tmp/shared-p03-root-review-20261005`.

## What Worked

The proposed recovery was limited to filename-derived titles, two to four
consecutive opening affiliation credits and a matching repeated running header.
It reused established affiliated-byline inference after normal trusted rules
failed, while passing title provenance through preparation/workspace helpers.

Both agent and coordinator audited 44 real All sources PDFs (3,484 total pages;
native metadata samples, not full fresh OCR). Exactly Mullins changed: blank
author became Ricky Mullins, Brooke Mullins; Review short label became Mullins.
The other 43 tracked author/title/short-label/provenance results were identical.

Actual original Mullins Automatic before/after workers both completed and were
ready: same PyMuPDF backend, five segments/page parents, no OCR. Identity-stripped
manifests matched and body exports were byte-identical. The agent's 129 existing
and focused checks passed before the additional independent negative tests.
These results alone did not establish safety.

## Why It Was Rejected

Coordinator constructed explicit non-author affiliation blocks: interview
subjects, conversation participants, advisory-board members and quoted experts.
All included the same two names in the filename-derived title and repeated
header. Baseline abstained; prototype assigned authors in all four cases.
Identity recurrence and affiliation do not prove the person's document role.

The agent independently reproduced those failures even with a trailing
structured filename credit suffix. Restricting to that filename structure alone
would therefore not solve the issue. A blacklist of the four test headings
would merely overfit the experiment rather than establish author-role evidence.

A fifth coordinator control with explicit translation credit on sampled page 3
also promoted both translators. The new helper re-inferred from page 1 only,
and its early return bypassed the existing all-sample translator exclusion.
The agent independently reproduced a mixed author/translator case where the
existing exclusion should have retained only the non-translator.

## Outcome

No prototype source was integrated, committed to production, deployed or used
for research ingestion. Production author/title/short-label behavior remains
unchanged. All original PDFs and experimental evidence remain intact; no owned
experiment processes remain. P03 is deferred, not marked solved.

Next development needs positive author-role evidence or independently qualified
role/layout context, not a global relaxation of title protection. Editor-as-author
and whole-journal contributor policies were not changed. The prototype's early
successful report is superseded by REJECTION.md; the complete failed evidence is
retained so it cannot be mistaken for a deployment-ready result.
