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

For selections of up to five PDFs, preview inspects the selected files. Larger
selections list every PDF and its independent editors immediately, but start no
arbitrary first-five scan. **Process metadata for all files** requests a full
preview. Without it, Confirm still detects every PDF freshly during processing.
**Confirm and start run automatically** starts only after that explicit full
preview completes; neither checkbox changes the other.

Accepted person names shown in metadata are given readable capitalization
while preserving initials and already mixed-case names. A default short
citation label uses one surname for one author, both surnames for two, and
the first surname plus *et al.* for three or more. User overrides retain their
entered spelling. A missing recognized author outlines the Author and Short
citation label rows in red and contributes to the completed-preview count.
Slash-separated evidence that repeats the work title shows the accepted name
in the main Author evidence row; the original phrase remains in the technical
details. A candidate name is not automatically accepted merely because it
appears in evidence. Embedded titles with a recognizable doubled UTF-8
decoding error are repaired conservatively for display and title resolution.

If the file-picker selection changes during a scan, the heading and banner
warn that its metadata is stale. The newer selection can finish its own scan;
**Run again** explicitly scans the current selection if needed. An outdated
selection revision cannot authorize automatic start.

If a preview fails, the accordion heading reports the number of failures and
names the latest affected PDF and its position in the selected list. That PDF's
row explains the category: missing file, unsupported extension, empty file,
non-PDF contents behind a `.pdf` name, unreadable PDF, access denial, password
requirement, or another inspection error. A renamed text file must be exported
as a real PDF before Confirm. File-content checks for this explanation occur
only after PDF opening fails. The heading describes previewed files only, not the
later selected PDFs that have not been inspected yet.

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
