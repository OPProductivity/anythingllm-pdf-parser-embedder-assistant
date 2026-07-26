# Using AnythingLLM PDF Parser Embedder Assistant

## Choose the output mode first

Use **Create local files only** when you only need parsed text and segment
files. Use **Create local files without logs** when you want a compact folder
containing only the transcript and the selected segments. Use **Create local
files and upload to AnythingLLM** when you want the assistant to submit the
prepared text records to a workspace.

Every upload run pauses at a confirmation screen before it changes local output
or an AnythingLLM workspace.

The app checks whether AnythingLLM Desktop is reachable when a confirmed upload
run begins. If Desktop is unavailable, it can make one bounded local startup
attempt and waits for the API rather than assuming the window is ready as soon
as its icon appears.

## Choose segmentation for the question you want to answer

| Need | Recommended mode |
| --- | --- |
| Fastest single-document ingestion | All in one file |
| A distinct document for every source page | Whole-page chunks |
| Page addressability while respecting upload boundaries | Page – preserve automatically |
| More precise passage retrieval on dense pages | Shorter page-local passages |

Smaller passages can improve retrieval precision, but they create more prepared
records for AnythingLLM to process. Keep the mode that best fits the citations
you need rather than assuming that more segments are always better.

## Upload mode checklist

1. Open AnythingLLM Desktop.
2. Confirm the configured embedding provider can embed a small manual test.
3. In this assistant, select a workspace or choose **New workspace**.
4. Choose **All segments** to submit the prepared records, or **Custom range**
   for one PDF when you intentionally want prepared-record positions such as
   `1-3, 4, 9, 12-30`.
5. Confirm the run.
6. Read the final status literally: local preparation, document storage,
   vector observation, and runtime retrieval are different stages.

The overall progress percentage is aligned with the active run's
elapsed/remaining forecast. The stage line contains direct evidence, such as
`Desktop completed 8/15 page-parent files` or `8/15 page-parent vectors
confirmed`. A runtime retrieval diagnostic that times out does not block use
when exact searchable-vector evidence is already verified.

Custom range applies after the selected segmentation strategy is calculated. It
is intentionally unavailable for batch upload because a single position range
would be ambiguous across multiple PDFs.

## OCR and review-required results

Automatic extraction prefers the native PDF text layer. It escalates to OCR or
Unstructured processing when the document signals a meaningful extraction risk.
Review the output if the run reports OCR failure, uncertain reading order,
ambiguous provenance, or substantive text loss. A generated transcript is not
proof that every visual element was recovered faithfully.

## Find your files

Use **Open Generated Output Folder** after a completed run. For the compact
local mode, the opened folder contains the transcript and segments directly.
For normal local or upload runs, it also contains the run evidence needed to
understand preparation and verification decisions.
