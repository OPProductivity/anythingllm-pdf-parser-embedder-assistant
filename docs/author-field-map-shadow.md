# Reversible author field review

`author_metadata.field_map` is an optional diagnostic stage in offline PDF preparation. In Automatic mode, **Run additional author-field and OCR review (diagnostic only)** is unchecked by default. Selecting it runs this additional field review after the selected extraction and author recovery. It writes `author-field-review.json` beside the selected candidate. The stage does not change the author, short label, segments, upload text, or embeddings.

With either checkbox state, the worker may reuse a completed metadata preview's structured author verdict and evidence after verifying the PDF SHA-256, recognition effort, fallback setting, title and resolved author. A missing or incompatible record falls back to fresh inference. The worker still reads the PDF for extraction and required metadata. A manual author override is applied to the final decision after the reuse check. Enabling the diagnostic review does not bypass any of that review's own OCR crops, expert calls or evidence checks.

An enabled elaborate review keeps its detailed JSON in the private run-state directory, including `source-profile.json` and the selected candidate's `author-field-review.json`. The published Automatic local output folder remains flat and TXT-only. This review is not written to TXT file properties, inserted into the transcript body, or sent as an embedding payload. The run-state folder and published output folder share the same run ID.

## Evidence loop

1. Read positioned native PDF words with page, box, extractor block/line, and font provenance. Keep the original words. Propose overlapping line, gap, alignment, and stacked-line groups by word IDs; no group rewrites the source or excludes an alternative.
2. Mark possible title, author credit, affiliation, abstract, copyright, DOI, and contents fields. Record multiple page roles. The first four pages are sampled; front matter may extend the map to page twelve until a visible title and linked credit are found.
3. Compare the final selected author with an available OCR extraction. Flattened OCR can suggest a printed byline, but its field boundary is unresolved until positioned evidence is available.
4. Recheck a likely title/credit region with OCR. If a crop is empty or conflicts, try wider compositions within a three-call budget. OCR word boxes retain separate provenance. Re-run the author strategy under the native genre and an independently inferred OCR genre, recording differences.
5. Return corroborated, unresolved, candidate, or disagreement for review. OCR recognition of a surname at a native title-linked credit can corroborate; a body-text occurrence alone cannot. A one-letter OCR variation is uncertainty.

An OCR failure is recorded per crop, not stored as a permanent failure for a PDF. `max_ocr_calls` bounds cost; another preparation may retry. The compact saved review keeps only atoms participating in field proposals, plus counts for the sampled page.

## Validation and scope

`tests/test_author_field_map.py` exercises alternative groups, OCR crop retries and conflicts, flattened OCR boundaries, front matter, adjacent columns, multi-author and affiliation cases. `tests/test_opening_ocr_scope.py` exercises the production preparation hook.

`python -m benchmarks.author_field_shadow_eval --review REVIEW.json --sources SOURCES.json --output RESULT.json --limit 30` replays local reviewed PDFs without preparation, upload, or author promotion. Its reported verdicts are field-review diagnostics. They do not prove all metadata labels correct or all PDF outputs unchanged. Full preparation and output comparison must be reported separately.
