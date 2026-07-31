# Detailed instructions

This is the complete user guide for AnythingLLM PDF Parser and Embedder
Assistant. For a concise overview and installation link, start at the
[README](../README.md). For the exact AnythingLLM Desktop evidence and safety
contract, read [AnythingLLM integration](ANYTHINGLLM-INTEGRATION.md).

## Contents

1. [Install and start the app](#install-and-start-the-app)
2. [Choose files](#choose-files)
3. [Set output and workspace](#set-output-and-workspace)
4. [Choose extraction and segmentation](#choose-extraction-and-segmentation)
5. [Confirm and follow progress](#confirm-and-follow-progress)
6. [Understand completion](#understand-completion)
7. [Use Advanced diagnostics](#use-advanced-diagnostics)
8. [Troubleshoot safely](#troubleshoot-safely)

The [screenshot gallery](screenshots/README.md) contains every screenshot
formerly embedded in the README. Treat it as a visual tour, not a guarantee of
the exact layout or duration on your machine.

## Install and start the app

### Recommended Windows installation

1. Download [Install-AnythingLLMPdfAssistant.ps1](https://github.com/OPProductivity/anythingllm-pdf-parser-embedder-assistant/releases/download/v0.5.1/Install-AnythingLLMPdfAssistant.ps1).
2. In File Explorer, right-click it and choose **Run with PowerShell**.
3. Keep the PowerShell window open and follow the prompts.
4. Start the app with the new **Start AnythingLLM PDF Assistant** desktop
   shortcut.

The installer downloads the current public source archive over HTTPS, prepares
the required Python environment, and creates Start/Stop shortcuts. It asks
before offering a per-user Python installation through `winget`; it does not
silently install Python, AnythingLLM Desktop, Tesseract, or an embedding
provider.

Before an upload run, start AnythingLLM Desktop and confirm its configured
embedding provider works. The assistant prepares and submits records; it does
not configure provider credentials for you.

### Manual installation

For users who already manage Python and `pipx`:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
pipx install --force https://github.com/OPProductivity/anythingllm-pdf-parser-embedder-assistant/archive/refs/heads/main.zip
anythingllm-pdf-assistant shortcuts repair
```

Close and reopen PowerShell if `pipx ensurepath` changed your PATH. Start the
app with `anythingllm-pdf-assistant start --browser`.

### Local app availability

The browser tab is a client for `http://127.0.0.1:7860`, not the server itself.
After using the Stop shortcut, start the app again and refresh a stale tab if
it does not reconnect. A red local-app notice means the browser cannot reach
the PDF assistant; it does not mean AnythingLLM Desktop documents were changed
or deleted.

The browser checks local availability cautiously. A short reconnecting notice
can happen while Gradio replaces components or hands a long operation to the
server. Wait for recovery before starting another run. Visible controls in an
old tab are not proof that its server is still live.

## Choose files

Use **PDF files** for one or more directly selected PDFs. When there is a
valid selection, a compact header action with the tooltip **Use selected files
again** starts a fresh setup pass without reopening Windows' file chooser. It
does not resume, retry, or cancel a previously submitted AnythingLLM run.

Use **PDF batch** to choose a folder. The app recursively finds candidate PDFs,
checks their headers, and presents a selector. The displayed scan count is the
number of local files and folders inspected during discovery—not the number of
PDFs that will be submitted. Directory symlinks are skipped, and very large
scans stop at the displayed interactive PDF safety limit.

After a valid folder scan:

- **Retry selected PDF batch** begins a fresh setup pass for the currently
  checked PDFs.
- **Clear batch** removes the remembered folder path, scan manifest, selected
  PDFs, selector choices, and status; it never deletes source files.
- If there are no readable PDFs, the folder chooser remains available so you
  can immediately choose a different root.

The direct-file and folder pickers form one pending Automatic batch. If both
currently have valid selections, both contribute files. Clear the unwanted
selection before confirming. On a browser reload or localhost restart, the
folder scan is intentionally forgotten; choose the folder again rather than
trusting a stale page.

## Set output and workspace

Choose one output mode:

| Mode | What happens |
| --- | --- |
| **Create local files only** | A compact transcript/segment export with no run logs. Its one output folder is named after the selected PDF, or the first and last PDF in a batch. Nothing is submitted to AnythingLLM. |
| **Create local files with diagnostic logs** | Text extraction, selected OCR/segmentation, local output, and ordinary run evidence. Nothing is submitted to AnythingLLM. |
| **Create local files and upload to AnythingLLM** | Local output plus submission of prepared records to an explicitly selected workspace or a newly created workspace. |

The output-root chooser controls where local run folders are created. Each
diagnostic-log run receives its own subfolder. Compact local-only exports are
placed directly in the selected root instead. Keep the selected root reasonably
short: generated paths have a 250-character Windows-compatible safety limit.

The compact export itself contains only the prepared transcript and any chosen
segments, all at that folder's root. The app keeps its ETA calibration history
separately under `%LOCALAPPDATA%\AnythingLLM PDF Parser Embedder Assistant\private-run-history\timing-model`.
Those records contain timing and aggregate counts, not extracted PDF text or API keys.

For upload mode, choose an existing workspace or **New workspace for this
document**. A new workspace is created only after confirmation. The selection
does not prove that Desktop has stored or embedded any record.

## Save future Automatic defaults

The **Advanced** tab's **Edit future Automatic defaults** fold-down stores
reusable choices for later Automatic setups. This includes the output mode and
folder, fallback-title preference, AnythingLLM API URL, workspace target,
native upload scope and metadata strategy, simulation endpoint and workload,
extraction/segmentation settings, and download preferences. After Save, an
idle Automatic tab reflects those values immediately; a selected or running
job keeps its own snapshot.

The private profile never stores source files, document title/author/citation
metadata, API keys, generated workspace or document-folder names, custom
per-PDF upload page ranges, recovery state, or controls that write shared
AnythingLLM provider settings. If a saved workspace is later removed, it stays
visible as a previously saved target until you choose another one; readiness
checks prevent an invalid upload.

## Choose extraction and segmentation

Automatic mode evaluates the source text and OCR risk. It can use native PDF
text, targeted/full Tesseract OCR where appropriate, or Unstructured-assisted
extraction for difficult layout. Extraction remains imperfect for some scans,
tables, images, and multi-column reading order; inspect output before relying
on it.

| Segmentation mode | What it prepares |
| --- | --- |
| **All in one file** | One record for the complete PDF. AnythingLLM may split it internally, so page-level identity is not promised. |
| **Whole-page chunks** | One prepared record for each extracted source page. |
| **Page – preserve automatically** | Source-page records, with local splitting only when a page exceeds the effective upload boundary. The records retain the page identity. |
| **Shorter page-local passages** | Smaller passages that remain tied to a source page. More passages can mean more Desktop/provider work. |
| **Custom Range** | Contiguous source-page ranges. A single positive number repeats; a comma list repeats in order for each PDF. |

For **Custom Range**, `20` produces `1–20`, `21–40`, and so on. `20, 30, 20`
produces `1–20`, `21–50`, `51–70`, then repeats that pattern if more pages
remain. The pattern starts at page 1 for every PDF in a batch; the final group
may be shorter. Blank, zero, negative, and malformed inputs are rejected. A
valid group larger than a PDF simply creates one range.

Custom Range is different from the upload-only **Custom range** scope. The
latter selects prepared records from one PDF and is unavailable for a
multi-PDF upload; it is not the setting that creates multi-page groups.

## Confirm and follow progress

After reviewing files and settings, select **Confirm and start processing**.
The button may briefly display a confirming state while the app freezes the
pending choices into the run snapshot. Editing the visible form afterward must
not rewrite that active snapshot.

The single progress bar is an evidence-based forecast, not a total of serial
steps. Queue observation and vector confirmation can overlap, so the app must
not double-count them. Read the stage line for the current activity:

- local preparation or page extraction;
- prepared page-parent records accepted by AnythingLLM;
- app-owned Desktop queue events;
- exact page-parent or page-range-parent vector confirmation; or
- reconciliation/recovery observation.

The ETA is an estimate, not a deadline. It can change when Desktop queue or
vector evidence materially changes. A quiet or reconnecting SSE observer is a
diagnostic condition, not automatic proof of a failed upload.

For multi-PDF runs, the UI also maintains a monotonic completed-document count.
A current-document reconciliation counter may restart from zero at the next
PDF; that does not mean an already completed PDF was lost.

## Understand completion

The app records four distinct kinds of evidence:

1. **Prepared locally:** transcript and selected segment records exist.
2. **Stored:** expected records reached the workspace/document path.
3. **Vector confirmed:** exact expected current-run page-parent or page-range
   parent identities have matching vector/provenance evidence.
4. **Runtime retrieval verified:** an optional live search returned matching
   content.

For ordinary page-aware upload, exact vector evidence is the normal success
boundary. An optional final retrieval diagnostic can time out or be deferred
after that proof without delaying completion. **Document(s) ready in
AnythingLLM** means the expected page-aware records are ready to use there;
local-only modes use **Document(s) ready to go**.

An accepted HTTP request, a queue update, or one Documents-drawer row alone is
not embedding proof. Conversely, a receipt delay, temporary SSE reconnect, or
deferred optional retrieval check is not automatically a failure after exact
vector evidence is confirmed. Use **Run output and downloads** to read the run
summary and diagnostic artifacts.

## Use Advanced diagnostics

The **Advanced diagnostic run** is a local, single-PDF diagnostic tool. It
gives direct control over extraction backend, OCR/deep extraction choices,
matter inclusion, page boundaries, segmentation, validation phrases, and
evidence retention. It does not upload to AnythingLLM, create or alter a
workspace, or edit the current Automatic job. It is useful for understanding a
difficult source before starting an ordinary Automatic upload; it is not a
retry or recovery mechanism for an already submitted run.

The **Edit future Automatic defaults** fold-down section opens a separate,
non-run editor from Advanced. Save stores only safe future preparation/output
preferences for the current Windows user and immediately reflects them in an
idle Automatic form. Cancel leaves them unchanged. The editor never stores
PDF selections or inferred metadata, API keys, generated per-document
workspace or document-folder names, custom upload ranges, diagnostics,
recovery state, or a current run. It does retain the reusable API URL and
workspace target selected for a later upload.
Saved defaults appear immediately in an idle Automatic form and apply to a
future selection; they never rewrite a selection already on screen or a
running job.

## Troubleshoot safely

- Run `anythingllm-pdf-assistant doctor` for writable-path and local-port
  diagnostics, and `anythingllm-pdf-assistant paths` to show the active app
  directories.
- Verify the embedding provider inside AnythingLLM Desktop before assuming a
  slow global queue is an assistant failure.
- If a folder scan count seems surprising, read the selected-root line. It is
  traversal work, not the number of selected PDFs.
- Reselect a batch folder after a browser/server restart.
- Read a failure report for the actual failed stage. Do not infer failure from
  an ETA, one quiet observer interval, or one Documents-drawer row.
- Do not publish provider keys, private PDF text, source paths, Desktop
  storage, workspace data, or raw local run timelines.

## Optional Desktop refresh bridge

The optional refresh bridge patches an installed AnythingLLM Desktop archive
only after anchor validation and a timestamped backup. Close AnythingLLM
Desktop before changing it, retain the backup until normal operation is
confirmed, and read [AnythingLLM integration](ANYTHINGLLM-INTEGRATION.md)
first.

```powershell
anythingllm-pdf-assistant bridge validate
anythingllm-pdf-assistant bridge install
anythingllm-pdf-assistant bridge upgrade
anythingllm-pdf-assistant bridge uninstall
```
