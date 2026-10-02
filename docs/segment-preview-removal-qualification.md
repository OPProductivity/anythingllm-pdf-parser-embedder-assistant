# Segment Preview Removal

The user requested retirement of the per-segment text/storage preview and
navigation, with an output-folder section in its place.

## Contained Changes

- Removed the Segment preview accordion, its number/text/storage controls,
  and all seven registered preview/navigation callbacks.
- Removed five dedicated Gradio preview helpers and their two unused imports.
- Removed the dedicated pipeline storage-preview helper, which had no other
  repository callers. Storage inspection and exact vector verification remain.
- Removed only the final three preview outputs from the selection-reset tuple
  and its shared output list. Normal and guarded returns now have sixteen
  outputs; the other stream and confirmation contracts are unchanged.
- Moved the existing generated-output action into an Open output folder
  accordion. Its target selection, lost-browser-event recovery, availability,
  and Explorer callback are unchanged. The hidden download/report components
  remain mounted for their established processing stream contracts.

Segmentation, manifests, export filenames/content, cache identity, duplicate
protection, upload, workspace selection and other UI sections were not changed.
AST comparison with the preceding commit confirmed that all remaining pipeline
definitions are identical; the only modified existing Gradio function is the
presentation reset with its three obsolete outputs removed.

## Verification

141 targeted and adjacent regression tests passed, including reset guards,
registered callback output counts, component/dependency integrity, existing
output-folder resolution and stale-browser-state recovery, picker transport,
worker protocol, history, output separation and server lifecycle. Ruff,
configured core type checking, whitespace and secret checks passed.

The restarted production server processed three custom-text PDFs in local-only
page-preserving mode: run `r-20261002-173449-66b52bf184` completed successfully.
All three complete-document TXT exports and six page-segment TXT exports were
present. The new accordion revealed the existing enabled generated-folder
button. Retry retained all three files and restored defaults; Clear removed
them; a different three-PDF selection restored editable controls and Confirm,
without a page reload between these actions. Preview controls were absent.

This bounded UI qualification did not upload or mutate AnythingLLM data.
Local test artifacts and the browser screenshot are retained in ignored
`tmp-output/reset-sequence-qualification`.
