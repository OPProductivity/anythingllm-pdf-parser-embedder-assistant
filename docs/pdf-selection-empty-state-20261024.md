# Empty-selection status and bounded port review

Clearing an ordinary picker or removing every folder chip could leave the page
saying "Ready — Confirm to begin processing" while Confirm was disabled. The
folder signature and finish guard also used the entire discovered set when an
explicitly selected subset was empty. Discovery is not selection.

An explicit empty subset now remains empty in the signature and readiness
acknowledgement. Initial, reset, action-row and final empty-selection views show
"No PDFs selected" with an idle state. A separately selected ordinary file still
enables Confirm, and obsolete transaction guards remain in force. No author
recognition, acquisition, OCR, extraction or embedding rules were changed.

Four focused controls reproduced the previous behavior before the fix. The
selection, picker, worker and adjacent structural/reset suite then passed 123
tests, including seven empty-selection controls. The real browser checks use at
most two PDFs per selection; single-PDF processing checks are separate. These are
bounded regression checks, not a new corpus or speed benchmark.

Both normal servers passed a real two-file upload, removal to one file and Clear
to zero files without Refresh. Clear completed in the idle state. Each port also
completed one default eight-page PDF run: Patrick J. Deneen was recognized from
the visible opening byline, eight records were newly attached to a workspace,
and all eight vectors were confirmed. Both integrity audits passed with zero
findings. These runs used eight existing embedding records each; no fresh paid
embedding request or live retrieval test was performed. The previous Desktop
SQLite lock was absent in the read-only schema check and these small runs; no
Desktop restart was needed.

## Recognition version and accepted runtime

The user accepts the displayed 1.754x level instead of pursuing 1.7x. This is a
runtime acceptance preference, not a switch in the application. The historical
1.754x result belongs to candidate22. Production contains candidate24, including
the later-role reconciliation repair, with the same reference-list recognition
scores. Candidate24's existing completed paired-control and retained historical
comparisons are distinct records; they are not relabelled as a new 1.754x
measurement. No further corpus speed test was performed for this port review.

Port 7860 uses current production recognition. Port 7861 keeps its frozen
recognition base and receives the narrowly qualified UI overlay. Passing the
selection checks or one PDF does not establish universal metadata accuracy,
large-batch ingestion, retrieval quality or complete manual corpus validation.
