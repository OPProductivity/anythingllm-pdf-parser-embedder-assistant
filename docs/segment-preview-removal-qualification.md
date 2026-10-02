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

## Standalone Folder Button

The subsequent user correction removes the Open Output Folder accordion
wrapper entirely. The existing button remains at the same top-level location,
with centered white text, a gray background and a white border. Its identity,
callback, visibility updates, state inputs and folder resolution are unchanged.
The component test now checks that it has no accordion ancestor.

The latest two user runs were inspected before this edit: the 17:42 run
(`r-20261002-174214-6ed75fa968`) used whole-file uploads to the Harkins workspace
and confirmed three records. The 17:45 run (`r-20261002-174505-94101bd49f`)
used page-local passages in `second-time-but-different-workspace`, preparing
214 records with a passage target of 750. Both used the same source hashes,
all 47 pages and effective embedding chunk size/overlap 8191/20. The second
run's terminal warning records incomplete vector confirmation within the
bounded reconciliation window, not identical segmentation settings.

63 focused regression tests and all configured pre-commit checks passed.
Successful-run lean retention means not every original first-run checkbox
value remains available; no missing settings were inferred from the GUI.

The idle production assistant was restarted and a preserved custom-text PDF
was processed in local-only mode. The browser showed a successful run and one
enabled Open Output Folder button, with no accordion ancestor. Computed styles
confirmed gray RGB(55,65,81), a white solid border and centered text. Screenshot:
`tmp-output/reset-sequence-qualification/standalone-output-folder.png`.
Neither user run was retried; AnythingLLM data was not mutated by this check.
