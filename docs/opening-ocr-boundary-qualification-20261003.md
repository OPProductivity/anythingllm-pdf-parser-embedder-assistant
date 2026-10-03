# Opening OCR scope and canonical diagnostic directions

## Observed failure

Run `r-20261003-162544-e28b3f2caa` prepared The Racial Middle with front matter included and no page override. Native extraction could not read the scanned cover and proposed page 2 as the first nonempty page. Its otherwise reliable extraction fixed the shared boundary. OCR recovered 13 words on page 1, but inherited page 2 and excluded that recovery.

This was selection-policy loss, not a worse OCR recognition algorithm. The shared-boundary mechanism predates the recent logging and confirmation changes.

## Contained correction

When front matter is included, verified preflight opening OCR targets now reserve their physical pages before a native candidate establishes the shared range. Every candidate keeps the same scope for quality comparison. Explicit first-page overrides still take precedence; front-matter exclusion and end-page selection are unchanged. No OCR recognizer, model, crop, reading-order rule, interface or output cleanup behavior changed.

The correction uses the existing all-page preflight evidence, not a title guess or a blanket inclusion of empty pages. This qualification specifically covers the Automatic production route with verified opening OCR targets; it does not establish every possible boundary case in direct calls without preflight evidence.

## Validation

- Six deterministic production-preparation cases cover whole-file and page-preserving modes, front matter included, explicit page-2 override and front matter excluded.
- Three diagnostic cases cover canonical layout and supplementary-lane paths.
- Twenty-two adjacent pipeline tests passed, covering targeted visual OCR, front matter, boundaries, diagnostics and clean primary output.
- Sixty artifact, private evidence, runtime event retention and local output finalization tests passed.
- A fresh execution through the actual cancellable production worker used the same book and original settings, with uploads and vector evaluation disabled and new OCR checkpoint paths. Both candidate boundaries are now page 1. The selected Unstructured candidate retains the OCR cover; the native book body is byte-identical after removing the new cover prefix. No workspace or embeddings were created, so no vector cleanup is needed.

The recognized cover remains imperfect: OCR returned "Latinas" where the source title is "Latinos". This change preserves recognized content; it does not claim perfect transcription or alter production OCR.

## Diagnostic-path investigation

All nine PDFs in the observed run had an unresolvable `selected/layout-region-review.json` instruction. Canonical indexes still resolve the retained candidate artifact. The artifact consolidation updated storage/reader paths but left three hard-coded diagnostic messages untouched.

Related edge-case report messages also used bare layout/retrieval-lane filenames, and supplementary-lane diagnostics used a bare filename even though the artifact can live under a candidate directory. These writers now use canonical role paths; callers without an explicit role path are directed to the stable artifact-locations index. Historical run files are not rewritten.

## Repeated book text, measured

Each of the following files contains exactly the same 612,100-byte UTF-8 book body:

| Relative path inside the book's private run directory | File bytes |
| --- | ---: |
| `candidates/pymupdf/segment-manifest.jsonl` | 639,163 |
| `candidates/unstructured/segment-manifest.jsonl` | 639,168 |
| `page-parent-manifest.jsonl` | 615,682 |
| `metadata-api/raw-text-payloads-strict.jsonl` | 615,448 |
| `metadata-api/raw-text-payloads-page-parents-strict.jsonl` | 615,434 |
| `metadata-api/raw-text-payloads-page-parents-native-header.jsonl` | 615,453 |

The six files total 3,760,348 bytes, including 3,672,600 bytes of identical body text. Keeping one shared body instead of six could avoid approximately 3,060,500 body bytes before reference overhead. Referencing the already canonical TXT body instead could avoid almost all 3,672,600 bytes, but would require changing JSONL readers, upload-plan reconstruction and manual-kit resolution, not simply deleting files.

The files are not byte-identical as complete records: backend identity, page spans, metadata contracts and parent identities differ. Those distinctions still serve production preparation and manual checks. Share text bodies while retaining their records; do not remove the workflow. This remains an optimization finding, not an implemented manifest-format change.

Separately, the run has 1,300,746 bytes of byte-identical private redundancy, primarily seven surplus copies of the 149,820-byte global `inspection/lancedb-before.json` observation. This is separate from the repeated body total above. Public exports are intentional standalone files and are not candidates for removal.

## Preflight timing limitation

The saved timeline records 15.528 seconds for confirmation preflight, but not the per-source timestamps needed to allocate that time to identity hashing, representative-page profiling, all-page native inspection, OCR capability import/probe, request validation and estimate calculation.

The callback builds those timestamps in memory and hands them to orchestration. Orchestration passes only their count and the overall duration to the timeline writer; its fixed schema does not retain even those preflight-specific count fields. The full rows do not appear in the inspected private JSON records. The per-source callback predates this week's serialization changes; compact JSON itself did not remove these rows.

Thus Desktop document completion history is complete in this run (36 retained runtime events), but detailed preflight event history is not retained. The earlier large-run runtime tail cap remains a separate limitation. A compact, once-written preflight stage journal would address timing attribution without changing ETA or adding repeated PDF text. No preflight/ETA code was changed here.
