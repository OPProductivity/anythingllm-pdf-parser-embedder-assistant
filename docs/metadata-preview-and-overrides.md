# PDF metadata preview and independent overrides

Every selected PDF has its own title, author, citation-label and Publisher
override fields. An empty field follows that PDF's detection; a filled field
is applied to that PDF when Confirm starts processing. Editing one source
does not create shared metadata for the other selected PDFs.

The same contract applies to multiple files chosen in the ordinary file picker
and to a folder batch. Drafts are keyed by normalized source path, rather than
the PDF's position in the list. Confirmation filters the draft payload against
the current selected paths and the four supported fields before preparing
each source with its own values. Preview suggestions are not user overrides.

Editors are available while other documents are waiting or being inspected.
They retain their values and active field/caret when preview HTML is replaced.
Values typed before a source is selected attach to the first source, rather
than automatically becoming shared values for every batch member.

The preview retains its existing limit of five fully inspected PDFs per
selection. All selected PDFs are listed and editable immediately. Sources
beyond the preview limit say **Not previewed** and are detected freshly during
confirmed processing. This limit does not limit which PDFs are processed.

Each source is identified by its filename. There is no repeated "Detected PDF
metadata" heading or unused Refresh button. The information button reveals
additional rows in that source's table.

## Clearing a batch

Clear batch resets the picker selection, metadata tables/status, and override
drafts, then restores the folder chooser. It does not delete the source PDFs
or previously saved AnythingLLM documents.

Clearing owns a new selection revision. Both the folder discovery/page scan
and the author-preview stream check that revision before publishing results.
An older stream cannot restore the selection or preview after it is cleared.
The clear action remains unavailable to overwrite an active confirmed run.

## Optional embedded Publisher metadata

A PDF can contain a malformed XMP reference while its standard metadata and
native page text remain readable. An exception in that optional Publisher
read is recorded in `publisher_error`; it does not discard the rest of the
source metadata or prevent author evidence acquisition. Genuine preview errors
are marked explicitly so the batch header reports a warning.

## Verification

Focused tests cover source-path filtering, blank-field fallback, cancelled
folder scans, and malformed XMP that points to a non-stream object. Browser
checks pause the second PDF's actual metadata inference, click and edit both
PDFs' controls, release detection, then run real local preparation and compare
the two saved source profiles. The folder tests supply only the native dialog's
chosen directory; discovery, page inspection, preview, and preparation execute
normally. Clear is checked during the paused preview and after completion,
including attempts by the superseded stream to publish late results.

The live two-PDF upload check uses a new AnythingLLM workspace. It compares
distinct overrides and blank-field fallback in stored document JSON, workspace
SQLite records, and the actual LanceDB vectors. It does not measure broader
author-recognition accuracy or retrieval-answer quality.
