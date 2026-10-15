# Local GROBID evidence for scholarly PDFs

The parser calls GROBID only when the existing document profile is
`scholarly_article` with `single_work` scope. Books, chapters, reviews,
dissertations, reports, and whole issues continue through their existing
strategies. GROBID runs as a separate local service; absence or failure leaves
the current author path intact.

The default URL is `http://127.0.0.1:8070`. An explicitly configured
`PDF_GROBID_URL` can point elsewhere. A local CRF service can be started with:

```powershell
docker run -d --name codex-grobid-scholar --restart unless-stopped --init --ulimit core=0 -p 127.0.0.1:8070:8070 grobid/grobid:0.9.1-crf
```

In the UI, `processHeaderDocument` supplies article title, author names,
affiliations, DOI, journal, publication date, abstract, and keywords. During
preparation, one `processFulltextDocument` call supplies these fields together
with structured references, in-text citation targets and surrounding paragraph
contexts, section headings, ordered paragraphs, footnotes, figures, tables,
formulas, and PDF coordinates. Fulltext also requests sentence segmentation
and component IDs, so each retained sentence can be linked to its paragraph
and page coordinates. Citation links retain the raw reference when GROBID
cannot extract a reference title.
The result and author comparison are retained in `source-profile.json` under
`grobid_analysis` and `grobid_author_decision`. Existing selected extraction
and TXT content are not replaced by GROBID's TEI.

The fulltext response already includes the article header, parsed references,
and citation callouts. Calling the separate reference, name, date, affiliation,
or PDF-annotation endpoints on the same article would repeat that work.
Patent parsing and model-training APIs serve different tasks and are not part
of article preparation. Crossref consolidation stays off by default until its
matches can be validated against the selected work independently.

Both calls default to `consolidateHeader=0`; fulltext also defaults to
`consolidateCitations=0`. This keeps PDF-extracted claims separate from
external Crossref metadata. `PDF_GROBID_HEADER_CONSOLIDATION` and
`PDF_GROBID_CITATION_CONSOLIDATION` can explicitly enable GROBID's documented
consolidation modes; the mode is recorded with each analysis. A GROBID author
fills an unresolved article only
if its title and every name are visible on the opening pages. It may expand a
matching filename surname. A different existing author is retained and the
conflict is recorded. This evidence grade is not a calibrated probability.

`tests/grobid_shadow_eval.py` compares current and assisted authors with the
hash-bound manual labels. For example:

```powershell
python tests/grobid_shadow_eval.py --pdf-dir 'C:\path\to\sources' --output "$env:TEMP\grobid-shadow.jsonl"
```

The CRF image is about 500 MB compressed; the full deep-learning image is
much larger and may be slower without a supported GPU. GROBID requires a text
layer. Existing OCR selection remains responsible for scanned documents;
GROBID's raw result is never taken as proof that the selected author is right.

## Local qualification (2026-10-15)

The hash-matched 64-PDF manual-label cohort contained 38 single-work scholarly
articles. The existing author path and the CRF-assisted path each matched 36
of those 38 complete author lists; one remained unknown. GROBID agreed with 25
existing assignments and did not override conflicts. A live preparation run
retained the chosen author and GROBID's structured references, citations, and
positioned sentences in the final source profile without uploading anything.

The full image was trialled separately on the two outstanding scholarly author
cases and two controls. It did not correct either outstanding case. On one
control it split two legitimate bibliography entries into four fragments,
inflating the reference count, and its fulltext calls were slower. The CRF
image therefore remains the active local default. These sampled results do not
establish accuracy on every article or on document genres excluded by the gate.

A second local, user-reviewed source list identified two single-work articles
that the genre classifier had routed to `generic`. Their first pages have a
compact title and byline followed by substantial scholarly prose, confirmed by
an affiliation or author-year citation. The conservative fallback cue now
routes those layouts to `scholarly_article`; GROBID recovers both complete
author lists. Under surname-level scoring, the 41 scorable local PDF rows rose
from 35 to 37 correct, with no other selected author changed. The separate
64-PDF manual-label sample retained its prior 12 author mismatches and two
genre mismatches. One whole journal issue and two sources without local PDFs
were not part of that 41-row source-author denominator.

## Documentation and ungated baseline audit (2026-10-15)

The REST calls use GROBID's documented multipart `input` field, TEI XML
response, `processHeaderDocument` for preview, and `processFulltextDocument`
for preparation. Header processing normally considers only the first two PDF
pages; its result is not guaranteed to equal fulltext processing. The client
now retries one HTTP 503 after the documented 2-second header or 5-second
fulltext wait. HTTP 204 is recorded as `no_content`, not service unavailability.
Both consolidation settings remain zero, so extracted metadata is not silently
replaced by a Crossref record.

The prior 64-PDF shadow evaluation was genre-gated: it did not send all 64 PDFs
to GROBID. A separate diagnostic header-only pass sent all 42 available PDFs
from the user-reviewed Thesis 2 workbook without the genre gate. Of 41 rows
with a single comparable suggested author list, raw GROBID surnames matched
18; 20 returned a different or empty author list, and three returned HTTP 500
with GROBID's `[NO_BLOCKS] PDF parsing resulted in empty content`. These raw
results were never promoted to source authors. GROBID itself documents its
article focus and warns against relying on it for whole books. In the normal
preparation path, 18 scholarly PDFs received fulltext processing. Their raw
GROBID author lists matched 12/18 suggested lists, whereas the guarded final
source authors matched 18/18. This is a cohort result, not general accuracy.
The ungated per-PDF results are saved alongside the reviewed workbook as
`grobid-unfiltered-header-baseline-20261015.jsonl`.
