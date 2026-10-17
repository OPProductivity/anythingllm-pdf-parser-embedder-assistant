Publishing research catalogue and transfer trials

proposed-variants.json contains 500 proposed semantic wording/layout templates,
20 in each of 25 categories. They are research hypotheses, not 500 quotations,
observed counts, production regexes or trained probabilities. sources.json
records 24 consulted publishing, style, metadata and university sources.
contextual-priorities.json records 20 situations in which role and work scope
change an expert's contribution. Experts may share visual, native and OCR cues.

The complete manually verified ground-truth workbook, source renderings, PDF
research downloads, literal code comparisons and per-case traces are preserved
in the local project output publishing-evidence-completion-20261017. Private
PDFs and the workbook are not included in this repository.

The implementation in this revision enables only selected bounded relations
that were manually compared with pages first: title-linked professional role,
affiliated byline without an Abstract label, and personal ownership roster
corroborated by a separate opening article credit. Native whitespace around
superscript markers remains significant. The contiguous-band expert must defer
when further author-affiliation pairs continue before prose.

Qualification results are recorded in the accompanying benchmark note after
the corrected candidate completes. The initial wider candidate returned
608/770 (8 gains, 7 regressions from 607); it was not adopted. Its failures
were coauthor truncation across three ordinary layouts, not exceptional PDFs.
The corrected boundary passed a focused 17/17 original/neutral filename replay.

Limits: this is development-reviewed metadata inference, not a blind holdout,
full PDF preparation, upload/embedding or installed Desktop runtime check.
770 filename cases represent 383 byte-distinct contents and fewer independent
works. Research PDFs are not silently added to the denominator. The long-term
target remains near 760/770. No GROBID or source-name exceptions are used.
