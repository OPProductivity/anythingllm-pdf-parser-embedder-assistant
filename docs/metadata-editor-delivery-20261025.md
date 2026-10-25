# Show independent metadata editors before page inspection

Selecting a folder used to populate the batch filenames, then inspect every
PDF's pages before the metadata preview callback created its document tables.
The accordion could consequently show a single generic editor throughout a
long batch page check. Ordinary uploads merged into a folder had the same gap.

The selection callbacks now publish one keyed table per selected document as
soon as its paths are available, before page inspection or author inference.
The detected-title cell initially shows the filename stem; other detected
values show a loading state. All four override inputs remain editable. The
existing browser transport restores each document's edits when detected
results replace these initial tables, and late defaults do not clear those
editable transports. Exact uploaded copies use the same canonical path keys
as the subsequent batch merge. Independent existing folder sources remain
independent, even when their bytes and basenames match.

The immediate loading pass preserves known filename suggestions. Stale or
cleared selection callbacks cannot republish their old tables, and an active
confirmed run prevents next-selection updates.

Coverage includes an eleven-path selection with 44 enabled editors before PDF
inspection, folder-stream delivery before page checks, stale selection guards,
uploaded-copy identity handling, callback ordering, and a browser check of
independent edits surviving HTML replacement. A separate isolated running app
was exercised through the ordinary multi-file picker with two generated
scanned PDFs. Filename suggestions appeared before detection, first-document
author and second-document title/Publisher edits survived detection, and all
eight fields remained editable afterward. These are UI and transport checks,
not a manual author-correctness study, full ingestion test, or embedding test.

Author recognition, OCR acquisition, expert priorities and saved-document
override application are unchanged. The existing five-document pre-Confirm
recognition preview limit is unchanged; every selected document nevertheless
has editors immediately, including documents inspected freshly at Confirm.
The complete metadata corpus was previously qualified in e1232e5; it is not
rerun for this presentation-only change.
