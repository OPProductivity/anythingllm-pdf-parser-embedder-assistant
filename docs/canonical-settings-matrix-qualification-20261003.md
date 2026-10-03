# Canonical settings matrix qualification

Five additional real PDFs were processed through the production preparer:
Miriam Hansen, Early Silent Cinema (nine pages); David Bordwell, The Idea of
Montage (ten); Francesco Casetti, The Persistence of Cinema (ten); Thomas Schatz,
Hollywood: The Triumph of the Studio System (twelve); and Aparicio, Reading the
Latino in Latino Studies (seventeen). Sources span both requested source folders,
with text-layer scans and digital PDFs. Original source hashes remained unchanged.

## Matrix

Each PDF was tested with six combined profiles, against both the unconsolidated
non-private path and the canonical private path: 30 paired cases, 60 preparations.

| Mode | Target | Representation | Transport | Other variations |
| --- | ---: | --- | --- | --- |
| none | 4096 | segments | raw text | All matter; no markers |
| page | 2048 | page parents | file | Pages 2-7; markers; overlap 100 |
| page_limit | 1024 | segments | raw text | Strict metadata; full markers; deferred lean cleanup |
| page_passages | 350 | page parents | file | Pages 2-7; no markers; overlap 150 |
| passages | 700 | segments | file | Strict metadata; custom title/author/label; deep extraction |
| custom_page_ranges | 8191 | segments | raw text | Repeating groups of 2,3; all matter; no markers |

PyMuPDF was used across the scanned sources. PyMuPDF4LLM was also tested on the
two digital sources for the short-passage and custom-group profiles. The existing
embedder safety policy remained active rather than bypassing its chunk limits.

## Results

All 30 final paired cases passed. The comparison covered primary text bytes,
manifest records, upload-plan body hashes and metadata, effective segmentation
and extraction decisions, provenance paths, canonical-role resolution, absence
of filesystem links, relocated diagnostic ZIPs, and flat public TXT filenames
and bytes. Retained private receipts separately verified mode, page overrides,
overlap, transport, custom identity and chunk-size safety policy.

Two deep-extraction cases, Casetti and Aparicio, reached `needs_review` on both
paths. Both withheld successful-run cleanup with `run_needs_review`; consolidation
did not change that decision or the selected prepared content. For Casetti, the
existing scorer preferred Unstructured output flagged for ambiguous fragmentation
over the cleaner native candidate. That is a separate selection-policy observation,
not a canonical storage change or a newly approved OCR modification.

An initial forced PyMuPDF4LLM Hansen scan attempt produced no usable segments on
the unconsolidated reference path. Its artifacts remain under the initial test
root `C:\Users\Ninkear\AppData\Local\Temp\cm-_rs4boj3`. This is not a claim that
forced PyMuPDF4LLM works for every scanned PDF. Production OCR was left unchanged.

Initial harness checks incorrectly expected review runs to qualify for successful
cleanup, and initially attempted to inspect lean evidence after pruning. Those
test assumptions were corrected without changing production gates or retention.
Retry receipts preserve earlier attempts; JSON tuple/list normalization and unique
retry directories prevent resuming already-passed cases or overwriting evidence.

## Boundaries

These are real preparation/export tests, not GUI-click tests or live embedding
tests. No AnythingLLM writes, workspaces or vectors were created. No original PDFs
or historical assistant runs were modified. Production OCR helpers were compared
as ASTs against HEAD, and `rag_pdf_tools.py` remained unchanged.

Scripts: `experiments/canonical_settings_matrix_20261003.py` and
`experiments/validate_canonical_matrix_receipts_20261003.py`.
Receipts: `tmp-output/canonical-settings-matrix-20261003/results.json`.
An additional 26 focused canonical/archive/private-retention tests passed; Ruff
passed for both scripts.

## Manual Kit Follow-Up

After approval to retain manual checks without automatic duplicate TXT copies,
five additional paired preparations (ten preparations) repeated one distinct
settings profile per source: whole-file, page parents, page-limit, page passages,
and custom-labelled deep passages. All five passed. Private kit plans reference
existing canonical files, retain expected metadata/checklists, and contain no TXT
payload copies. The pre-existing lean mode still omits kits when appropriate.
Both complete relocated diagnostic bundles and public TXT export parity passed.
Legacy explicit standalone preparation retains portable kits; private automatic
run kit folders alone are no longer portable, as their checklists explain.

Receipts: `tmp-output/canonical-kit-followup-20261003/results.json`.
Run with `--kit-followup`; separate receipts preserve the original 30-case matrix.
Focused canonical, diagnostics and runtime-retention tests: 22 passed. Core
Pyright reported zero errors/warnings; production OCR source remains unchanged.
Pipeline regression suite: 919 tests and 15 subtests passed. One dependency
deprecation warning came from Starlette's TestClient/httpx integration, not a
failed application check; no dependency upgrade was made for this warning.
