# Reversible author field review

`author_metadata.field_map` is a diagnostic stage in offline PDF preparation. It runs after the selected extraction and author recovery, then writes `author-field-review.json` beside the selected candidate. The stage does not change the author, short label, segments, upload text, or embeddings.

## Evidence loop

1. Read positioned native PDF words with page, box, extractor block/line, and font provenance. Keep the original words. Propose overlapping line, gap, alignment, and stacked-line groups by word IDs; no group rewrites the source or excludes an alternative.
2. Mark possible title, author credit, affiliation, abstract, copyright, DOI, and contents fields. Record multiple page roles. The first four pages are sampled; front matter may extend the map to page twelve until a visible title and linked credit are found.
3. Compare the final selected author with an available OCR extraction and a same-work GROBID candidate. Flattened OCR can suggest a printed byline, but its field boundary is unresolved until positioned evidence is available.
4. Recheck a likely title/credit region with OCR. If a crop is empty or conflicts, try wider compositions within a three-call budget. OCR word boxes retain separate provenance. Re-run the author strategy under the native genre and an independently inferred OCR genre, recording differences.
5. Return corroborated, unresolved, candidate, or disagreement for review. OCR recognition of a surname at a native title-linked credit can corroborate; a body-text occurrence alone cannot. A one-letter OCR variation is uncertainty. An unverified GROBID work match cannot settle a disagreement.

An OCR failure is recorded per crop, not stored as a permanent failure for a PDF. `max_ocr_calls` bounds cost; another preparation may retry. The compact saved review keeps only atoms participating in field proposals, plus counts for the sampled page.

## Validation and scope

`tests/test_author_field_map.py` exercises alternative groups, OCR crop retries and conflicts, flattened OCR boundaries, front matter, adjacent columns, multi-author and affiliation cases. `tests/test_opening_ocr_scope.py` exercises the production preparation hook.

`python -m benchmarks.author_field_shadow_eval --review REVIEW.json --sources SOURCES.json --output RESULT.json --limit 30` replays local reviewed PDFs without preparation, upload, or author promotion. Its reported verdicts are field-review diagnostics. They do not prove all metadata labels correct or all PDF outputs unchanged. Full preparation and output comparison must be reported separately.
