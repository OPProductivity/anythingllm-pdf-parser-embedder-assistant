# Using AnythingLLM PDF Parser and Embedder Assistant

This guide describes the ordinary **Automatic** workflow. It is deliberately
conservative: every run pauses for confirmation before the assistant creates
local artifacts or changes an AnythingLLM workspace.

## Before an upload run

1. Start AnythingLLM Desktop and make sure its embedding provider works with a
   small manual document or workspace test.
2. Start this assistant from the **Start AnythingLLM PDF Assistant** desktop
   shortcut, or run `anythingllm-pdf-assistant start --browser`.
3. Open `http://127.0.0.1:7860` only after the local page has finished loading.
   The browser page is a local client; it is not an AnythingLLM Desktop health
   guarantee by itself.

The app can make one bounded attempt to start a local Desktop runtime when
needed. It does not change your embedding-provider settings or silently repair
an ambiguous Desktop queue.

### Local app availability and browser restarts

The browser tab is only a view of the local app. If the **Stop AnythingLLM PDF
Assistant** shortcut was used, the existing tab cannot begin a new upload or
run. Start the app again with the matching Start shortcut, then refresh the
tab if it does not reconnect by itself. A red connection notice means the
browser cannot currently reach the localhost app; it does **not** mean that
AnythingLLM Desktop documents were deleted or changed.

The app probes its local server cautiously so one delayed browser request does
not immediately look like an outage. A temporary reconnecting notice can occur
while Gradio replaces UI components or while a long action is handed to the
server. Wait for the green restoration notice or refresh the page before
starting another run. Never assume an old browser tab has a live server merely
because its form controls are still visible.

## Choose files

You can use either picker in the Automatic tab.

### PDF files

Use **PDF files** to select one or more PDFs directly. After a valid selection,
the header offers a compact upload-tray action with the tooltip **Use selected
files again**. It preserves the selected paths and restarts the fresh setup
flow, so you can revise settings without reopening the file dialog. It does
not repeat, resume, or cancel a previously submitted AnythingLLM run.

### PDF batch

Use **PDF batch** to choose a folder. The app recursively discovers candidate
PDFs, validates their headers, and then shows a selector for the PDFs that
belong in this run. The status line identifies the selected root folder and
reports the number of inspected items. That number counts files and folders
visited during the scan; it is not a PDF count and is not the number of files
that will be submitted.

After a valid batch is found, **Retry selected PDF batch** repeats the fresh
setup pass for the currently checked PDFs and **Clear batch** removes the
folder path, manifest, selected PDFs, choices, and status. If the folder has
no readable PDFs, the chooser remains available so you can select a different
root immediately.

The two pickers describe the same pending Automatic batch. If both currently
contain files, the assistant treats the valid direct-file selection and the
checked folder selection as one batch. Use **Clear batch** or the ordinary
picker's clear control before confirming if that is not what you intend. The
app never deletes source files from either picker.

On a browser reload or localhost restart, batch-folder scan state is cleared
intentionally. An old folder scan must not be treated as a fresh current
selection. If you had a batch prepared, choose the folder again. This rule is
stricter than the same-session reuse action because a scan result is local,
time-sensitive evidence.

Choosing a new folder or cancelling a folder chooser clears an incomplete
previous folder-scan state. If an earlier scan was interrupted by a restart,
select the folder again instead of relying on a visible old count. Folder scans
skip directory symlinks and stop at the displayed interactive PDF safety limit;
choose a narrower folder when a large library is truncated.

## Choose the output mode

| Mode | What the assistant does |
| --- | --- |
| **Create local files only** | Extracts text, applies selected OCR/segmentation behavior, and creates local output plus normal run evidence. Nothing is submitted to AnythingLLM. |
| **Create local files without logs** | Creates a compact local transcript/segment output. Use it when you deliberately want minimal local artifacts. |
| **Create local files and upload to AnythingLLM** | Creates local output, submits prepared records to an existing or new AnythingLLM workspace, and observes storage/vector evidence. |

For upload mode, choose an existing workspace or **New workspace for this
document**. A new workspace is created only after you confirm the run. The
workspace choice and generated name are part of the pending run settings; they
are not evidence that Desktop has completed embedding.

## Choose segmentation deliberately

The segmentation setting controls the local records prepared from each source
PDF. It does not override every splitter decision inside AnythingLLM.

| Segmentation mode | Use it when | Important behavior |
| --- | --- | --- |
| **All in one file** | You want the smallest number of local records. | AnythingLLM can still split the long record internally; page-level retrieval identity is not promised. |
| **Whole-page chunks** | You want one prepared record per extracted page. | A useful, direct source-page boundary. |
| **Page – preserve automatically** | You need page-aware retrieval but pages can exceed the upload boundary. | A large source page may become several local records, each retaining its source-page identity. |
| **Shorter page-local passages** | Dense pages need more precise retrieval. | Produces more records and can increase Desktop/provider work. |
| **Custom Range** | You want contiguous multi-page records. | One positive value repeats; a comma list repeats as a sequence for each PDF. |

For **Custom Range**, enter `20` to make `1–20`, `21–40`, and so on, with a
possibly shorter final group. Enter `20, 30, 20, 40, 60` to create uneven
consecutive groups and repeat that pattern when a PDF is longer than the
sequence. The pattern starts again at page 1 for every PDF in a batch. Blank,
zero, negative, or malformed values are rejected. A valid group size larger
than the PDF simply produces one group.

Do not confuse this setting with the upload-only **Custom range** scope. That
scope selects individual prepared records from one PDF; it is intentionally
unavailable for a multi-PDF upload because one record-position expression
would otherwise be ambiguous across documents.

## Confirm, then read the progress correctly

After choosing files and settings, use **Confirm and start processing**. The
button may briefly show a confirming state while the app freezes the pending
settings into a run snapshot. Once processing starts, changing visible form
controls must not rewrite that snapshot.

The progress bar is a forecast driven by current run evidence. Its number is
not a count of serial phases. Queue observation and vector confirmation can
overlap, so the bar must not add both as if they were separate completed work.
The stage line is more specific than the percentage and may say, for example:

- local preparation or page extraction is active;
- prepared page-parent records are being accepted by AnythingLLM;
- the Desktop queue has owned events for the current run;
- exact page-parent vectors are being confirmed; or
- a recovery/reconciliation observation is taking place.

An ETA is an estimate, not a deadline. It can be repriced when provider or
Desktop queue evidence materially changes. A reconnecting or quiet SSE
observer is useful diagnostic information, but does not by itself prove the
upload failed.

For a multi-PDF run, the UI also keeps a separate monotonic completed-document
count. The current document's Desktop reconciliation counter can restart from
zero when the pipeline moves to the next PDF; that is a per-document evidence
counter, not a claim that already completed PDFs were lost. Read the completed
batch count for whole-batch progress and the queue/vector line for the current
document's evidence.

## What “ready” means

The assistant distinguishes four evidence layers:

1. **Prepared locally:** extraction and segment files were written.
2. **Stored:** the expected prepared records reached the workspace path.
3. **Vector confirmed:** exact expected current-run page-parent identities
   have corresponding searchable vector evidence with matching provenance.
4. **Runtime retrieval verified:** an optional live search returned matching
   content.

For ordinary page-aware upload, exact page-parent vector confirmation is the
normal success boundary. A live retrieval diagnostic can time out or be
deferred after that proof without delaying completion. Read the completion text
literally: **Document(s) ready in AnythingLLM** means the document is ready to
use there; local-only modes use **Document(s) ready to go** instead.

Queue receipt delays, a quiet/reconnecting SSE observer, and an optional
retrieval timeout are not automatic failures after exact vectors have been
confirmed. Conversely, an accepted upload request or a Documents-drawer row
is not by itself proof of completed embedding.

## Review outputs and warnings

Use **Open Generated Output Folder** and **Run output and downloads** after a
run. Normal local/upload modes retain a run summary and evidence artifacts;
the compact local mode intentionally retains less. Review warnings that concern
OCR, reading order, ambiguous provenance, missing source text, or a failed
identity/vector check. A successful transcript is not proof that every table,
scan, visual element, or reading order was recovered perfectly.

The app deliberately avoids broad workspace audits as an ordinary success
blocker. Those audits, manual chat/citation probes, and optional retrieval
checks are diagnostics when something needs investigation.

## Advanced diagnostic runs

The **Advanced** tab is a local, single-PDF diagnostic tool. It offers direct
control over the extraction backend, OCR/deep extraction choices, matter
inclusion, page boundaries, segmentation, validation phrases, and evidence
retention. Use it to inspect a difficult source before choosing ordinary
Automatic upload settings.

An Advanced diagnostic run does not upload to AnythingLLM, mutate an
AnythingLLM workspace, or save a new Automatic default profile. Its output is
kept under the Advanced diagnostic output root selected in that tab. It is not
a recovery or retry mechanism for an already submitted Automatic run.

## Safe troubleshooting

- Use `anythingllm-pdf-assistant doctor` for startup/path diagnostics.
- Confirm AnythingLLM's embedding provider independently before treating a
  slow queue as an assistant error.
- If a folder scan has surprising counts, read the selected-root line first;
  the count is traversal work, not selected PDFs.
- If the local page reconnects after a server restart, reselect a batch folder
  rather than relying on a previously displayed batch result.
- If the output root is deeply nested, choose a shorter folder before
  processing. Generated run and export paths must remain within the app's
  Windows-compatible 250-character limit.
- Do not post provider keys, local PDF contents, absolute paths, AnythingLLM
  storage archives, or raw run timelines in issues.
