# AnythingLLM PDF Parser and Embedder Assistant

This Windows-local assistant turns one PDF, a selected set of PDFs, or a PDF
folder into inspectable text records for AnythingLLM Desktop. It can also stop
after local preparation, which is useful when you want to review the text or
upload it manually.

AnythingLLM can ingest PDFs directly. This project takes a different route
when page-aware retrieval matters: it prepares controllable text records with
source-page provenance before submitting them through AnythingLLM Desktop's
managed workspace path. That makes the application's claim modest and
testable: a page-aware record is only called ready after the expected
current-run record identities and their vector evidence have been confirmed.

The local web application runs on `http://127.0.0.1:7860`. PDF extraction,
OCR decisions, segmentation, local artifacts, and the app's own reports stay
on the machine. In upload mode, AnythingLLM Desktop then uses the embedding
provider configured inside Desktop. A local provider and a cloud provider have
very different privacy and retention policies; review the provider you chose
before using sensitive text.

The assistant supports native-text PDFs, selective or full Tesseract OCR, and
Unstructured-assisted extraction for difficult layouts. It examines PDF
metadata and document structure, can include or exclude front/back matter,
and keeps the generated output available for review. Preparation is often
quick; total upload time is commonly dominated by AnythingLLM Desktop's queue
and the configured embedding provider.

> **AI-assisted disclosure:** this project was completely vibe-coded through
> iterative AI-assisted development. Treat it as beta software: review the
> generated outputs and test it with your own AnythingLLM configuration before
> using it for consequential work.

> **Use at your own risk.** This software is provided under the MIT License,
> without warranty of any kind. It is not affiliated with, endorsed by, or
> supported by Mintplex Labs or AnythingLLM.



## Example Screenshots

<img width="570" height="612" alt="image" src="https://github.com/user-attachments/assets/f84ed39b-164f-403f-ad95-170a302487cf" />


<img width="567" height="611" alt="image" src="https://github.com/user-attachments/assets/2a92ddef-1db8-4809-b872-8304ff1bd47b" />
<img width="567" height="611" alt="image" src="https://github.com/user-attachments/assets/5049178f-14e6-4921-b7c7-a9b1c7a8e63b" />


<img width="810" height="493" alt="image" src="https://github.com/user-attachments/assets/a3a39db0-ba33-41d5-8732-4c5f87c7a14d" />


**No more this:**
<img width="1091" height="612" alt="image" src="https://github.com/user-attachments/assets/25b6d878-ff43-44a7-a888-4c19e230163d" />
<img width="1091" height="612" alt="image" src="https://github.com/user-attachments/assets/da471587-5b57-4cc7-bcf4-ae359a839fca" />


## Why use it?

- Prepare a whole PDF, one record per page, page-preserving records, smaller
  page-local passages, or explicit consecutive page groups.
- Select one PDF, several PDFs, or recursively scan a folder and choose the
  PDFs from that folder that belong in a batch.
- Keep extraction/OCR local and choose whether to create local files only or
  submit prepared records to an existing or newly created AnythingLLM
  workspace.
- Retain inspectable output, source metadata, page ranges, progress evidence,
  and a run report instead of treating one accepted HTTP request as success.
- Reuse a current selection to adjust settings without reopening the file
  picker. This starts a fresh setup pass; it never silently resumes an old run.
- Use light, dark, or Windows-following appearance settings and launch the
  application from Windows desktop shortcuts after installation.
- Optionally use the Desktop refresh bridge when a validated installation of
  that bridge is appropriate for the local AnythingLLM version.

AnythingLLM supports multiple document types, including PDFs. This assistant
intentionally prepares text records because controlling the text record and its
page metadata gives page-aware retrieval a clear, inspectable boundary.

## What it does

```mermaid
flowchart LR
    PDF["PDF or PDF batch"] --> Inspect["Inspect text layer and metadata"]
    Inspect --> Extract["Native extraction or OCR / Unstructured"]
    Extract --> Segment["Choose whole file, page-aware, or page-local segments"]
    Segment --> Local["Local text output"]
    Segment --> Upload["AnythingLLM Desktop workspace"]
    Upload --> Verify["Storage, vector, and retrieval checks         "]
```

### Output modes

| Mode | Result |
| --- | --- |
| **Create local files only** | Creates a local transcript, segments, and normal run evidence. It does not submit anything to AnythingLLM. |
| **Create local files without logs** | Creates a compact local transcript/segment output for a deliberately minimal local result. |
| **Create local files and upload to AnythingLLM** | Creates local output, then submits prepared text records to an explicitly selected existing workspace or a newly created workspace after confirmation. |

### Segmentation choices

- **All in one file:** one text record for the entire PDF. AnythingLLM may
  still apply its own splitter before embedding, so this mode does not promise
  page-level citation identity.
- **Whole-page chunks:** one record for each extracted page.
- **Page – preserve automatically:** prepare page-aware records and split an
  over-large page locally only when needed for the effective upload boundary;
  the resulting records retain the original page identity.
- **Shorter page-local passages:** create smaller page-addressable passages for
  dense pages where more granular retrieval is useful.
- **Custom Range:** enter one positive page count, such as `20`, for repeating
  20-page groups, or a comma-separated sequence such as `20, 30, 20, 40, 60`
  for uneven consecutive groups. The sequence restarts for each PDF and the
  final group may be shorter. Blank, zero, negative, and malformed values are
  rejected.

Custom Range segmentation is different from the upload-only **Custom range**
scope. The latter selects particular prepared records for one PDF and is not
available for a multi-PDF batch; Custom Range segmentation groups the pages of
each selected PDF and can be used for a batch.

The app does not overwrite or delete existing AnythingLLM workspaces. In upload
mode, select an existing workspace or choose **New workspace for this
document**. For a new workspace, the app proposes a safe name derived from the
document metadata; you can edit it before confirmation. The app records what
it observes when Desktop embedding is delayed, but does not claim a queue
receipt alone proves vectors are searchable.

## Quick start

### Requirements

- Windows 10/11
- [AnythingLLM Desktop](https://anythingllm.com/desktop) running locally when
  you use the upload mode
- An embedding provider configured and working in AnythingLLM Desktop (install Ollama on your PC or configure OpenRouter API key inside AnythingLLM)

You do **not** need Git, a cloned copy of this repository, Python, or `pipx`
before starting the recommended installation. The installer handles the Python
application requirements and asks before offering to install Python through
`winget`.

## Install directly like this:
The easiest installation uses one Windows PowerShell file. You only need to
download that file; you do **not** need to download the rest of this repository
or install Git first.

1. Download [Install-AnythingLLMPdfAssistant.ps1](https://github.com/OPProductivity/anythingllm-pdf-parser-embedder-assistant/releases/download/v0.5.1/Install-AnythingLLMPdfAssistant.ps1) to your **Downloads** folder.
2. In File Explorer, right-click the downloaded file and select **Run with PowerShell**.
3. Keep the PowerShell window open and follow its prompts. It tells you what it
   finds and what it needs before doing anything optional.
4. When installation finishes, use the new **Start AnythingLLM PDF Assistant**
   shortcut on your Desktop. It starts the local app and opens it in your web
   browser. The matching **Stop AnythingLLM PDF Assistant** shortcut stops it.
5. Done. You can now use the app. For your first run, simply choose a PDF and
   an output mode, then scroll down to the blue **Confirm and start
   processing** button. You can leave the remaining options at their defaults.

## How it works:

The installer downloads the current public `main.zip` source archive from
GitHub over HTTPS, installs `pipx`, and installs the application's required
Python packages. It is deliberately not a version-pinned GitHub Release asset:
a new installation gets the current public `main` version and needs an internet
connection while it installs. If a supported Python version is not available,
the installer asks before offering a per-user Python 3.14 installation through
`winget`; it never installs Python without your confirmation. It does not
require Git or a manually cloned repository, but feel free to clone the
repository, ask AI (Codex or Claude Cowork) to help you install it, or download
it with Git like this:

```powershell
git clone https://github.com/OPProductivity/anythingllm-pdf-parser-embedder-assistant.git
Set-Location anythingllm-pdf-parser-embedder-assistant
.\Install-AnythingLLMPdfAssistant.ps1
```

For PDF upload and embedding, you still need AnythingLLM Desktop and a working
embedding provider inside it. If the installer cannot find AnythingLLM Desktop
or Tesseract OCR, it explains what the missing component is for and asks before
opening the official download page. It does not silently install either
application or change your embedding provider settings.

The app automatically assesses each PDF during preparation. It can recognize
fully text-based PDFs, scanned or image-only PDFs, and pages containing images
whose text may benefit from OCR. Tesseract OCR is therefore only needed when
the document's OCR/readiness assessment indicates that reliable OCR is needed.

Only run the installer after reviewing the linked public source. If Windows
blocks a downloaded PowerShell script, right-click the file, choose
**Properties**, select **Unblock** if that option appears, click **Apply**, and
then choose **Run with PowerShell** again. If the right-click option is not
available, open PowerShell in the Downloads folder and run:

```powershell
.\Install-AnythingLLMPdfAssistant.ps1
```

The installer uses the packaged Start/Stop icons and points both shortcuts at
the installed pipx environment.
To repair the shortcuts later, run:

```powershell
anythingllm-pdf-assistant shortcuts repair
```

---

### **Advanced: manual pipx installation**

For users who already manage Python and `pipx` themselves:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
pipx install --force https://github.com/OPProductivity/anythingllm-pdf-parser-embedder-assistant/archive/refs/heads/main.zip
anythingllm-pdf-assistant shortcuts repair
```

The app also repairs the Start/Stop shortcuts automatically when it is first
started on Windows.

Close and reopen PowerShell if `pipx ensurepath` changed your PATH. Then start
the local app:

```powershell
anythingllm-pdf-assistant start --browser
```

The app opens at `http://127.0.0.1:7860`. Use these diagnostics if needed:

```powershell
anythingllm-pdf-assistant doctor
anythingllm-pdf-assistant paths
```

For a development checkout, run `pipx install .` in the repository root and
then `anythingllm-pdf-assistant shortcuts repair`.

## How to use the app

1. Start AnythingLLM Desktop and confirm that its embedding provider works.
2. Start this assistant with `anythingllm-pdf-assistant start --browser` (or use the desktop shortcut).
3. Upload one PDF, several PDFs, or select a batch folder and choose the
   discovered PDFs that belong in the run.
4. Review the detected PDF metadata and set title/author information if needed.
5. Select an output mode and segmentation mode. In **Custom Range**, provide
   a positive page-group size or comma-separated sequence.
6. For upload mode, choose an existing workspace or **New workspace for this
   document**.
7. Confirm the settings. The app freezes a run snapshot, creates local outputs before changing the
   AnythingLLM workspace.
8. Review the completion state and the generated output folder. For upload
   runs, distinguish local preparation, document storage, exact vector
   confirmation, and optional retrieval evidence in the run report.

For a detailed walkthrough, including batch scan counts, reuse behavior,
Custom Range semantics, and the meaning of each completion state, see
[docs/USAGE.md](docs/USAGE.md). For Desktop-specific safety and verification
boundaries, read [docs/ANYTHINGLLM-INTEGRATION.md](docs/ANYTHINGLLM-INTEGRATION.md).

## Screenshots

<img width="876" height="943" alt="image" src="https://github.com/user-attachments/assets/40432234-a23f-446a-95c9-143482da19b3" />


<img width="878" height="943" alt="image" src="https://github.com/user-attachments/assets/5cd601f9-0b8d-40ae-b357-be4f6a1145bf" />

<img width="870" height="944" alt="image" src="https://github.com/user-attachments/assets/ed1ac2fa-b586-4689-b9dc-2d5eb6129d17" />

<img width="875" height="943" alt="image" src="https://github.com/user-attachments/assets/de254721-137a-46fc-adce-ddd2a43a5c1a" />

<img width="872" height="942" alt="image" src="https://github.com/user-attachments/assets/69efd3ec-7762-4ec8-966f-423bd4078457" />

<img width="878" height="945" alt="image" src="https://github.com/user-attachments/assets/1b6876a6-59ae-4bd3-9828-85b947f3b414" />

<img width="873" height="466" alt="image" src="https://github.com/user-attachments/assets/d0964653-485f-4b86-9657-3a7772e43466" />


<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/ae143af4-2fe8-4c24-bcd9-3aa457bd61ee" />


<img width="1620" height="985" alt="image" src="https://github.com/user-attachments/assets/a40a4640-7975-4a50-9caa-d52201b942bb" />



## AnythingLLM integration

The upload workflow uses AnythingLLM's local HTTP APIs and its documented
workspace/document model where available. It deliberately keeps provider keys
inside AnythingLLM Desktop: no provider API key is read from, stored in, or
committed by this repository.

The optional refresh bridge is a separate, opt-in desktop integration. It
patches the installed desktop archive only after validating known anchors and
creating a timestamped backup. Close AnythingLLM Desktop before changing the
bridge.

```powershell
anythingllm-pdf-assistant bridge validate
anythingllm-pdf-assistant bridge install
anythingllm-pdf-assistant bridge uninstall
```

See [docs/ANYTHINGLLM-INTEGRATION.md](docs/ANYTHINGLLM-INTEGRATION.md) before
using the bridge or relying on a workspace for citations.

## Local data, privacy, and safety

- Generated output, logs, and local configuration live under
  `%LOCALAPPDATA%\AnythingLLM PDF Parser Embedder Assistant` by default.
- Set `ANYTHINGLLM_PDF_ASSISTANT_HOME` before launching to use another writable
  location, including a portable drive.
- Your PDFs remain local unless you choose AnythingLLM upload. Your configured
  AnythingLLM embedding provider may send text to the provider you selected;
  check that provider's policy before uploading sensitive material.
- Never commit `.env` files, AnythingLLM Desktop storage, run output, logs,
  private PDFs, API keys, or desktop backups.
- This project is not a security boundary, legal advice, a backup system, or a
  guarantee that any answer produced by an LLM is correct.

## Development

```powershell
py -m pip install -e .
py -m pytest -q
py -m pre_commit run --all-files
```

Contributions, bug reports, and reproducible test cases are welcome. Please
redact credentials, private PDF text, local paths, workspace data, and
AnythingLLM storage archives from any report.

## Related projects and official references

- [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm)
- [AnythingLLM documentation](https://docs.anythingllm.com/)
- AnythingLLM's local developer API documentation at `http://127.0.0.1:3001/api/docs`
- [pipx documentation](https://pipx.pypa.io/)

## License

MIT. See [LICENSE](LICENSE).

## Example: parsing and embedding a six-page, two-column PDF

Start:
<img width="1639" height="980" alt="2026-07-24_12h54_10" src="https://github.com/user-attachments/assets/902ddcb5-4bac-4c03-b6d8-1544dfa9a969" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/03b2a41b-e9cd-4386-95cc-dca3b63c859f" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/901975d1-0085-4001-b109-60fe091ef510" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/54838066-aaa8-4037-b8d3-c9dc446e2f16" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/71268e08-3271-41ff-9269-75ea2da3f90f" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/525e6ab0-9ca2-43f2-b633-6f1baece1b4d" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/640ac739-e423-44a9-9627-6345383dcfcf" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/a740739c-3462-413a-ac7c-39702aeb3ed7" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/5e9024f2-38a6-4d54-b215-4bccce7bd317" />
<img width="1920" height="1072" alt="image" src="https://github.com/user-attachments/assets/264add11-928e-42c3-aeae-030ff0cbbc90" />
<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/b929dbb9-331f-417a-b108-15f17c63762a" />
<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/41e244cb-761d-4240-b0cf-5ac2acbd2e48" />
<img width="1920" height="1072" alt="image" src="https://github.com/user-attachments/assets/42a383b1-f7b1-425f-a672-6fe083c6039d" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/8f3812af-3015-478f-8ef5-8a92d1ebf70a" />
<img width="1920" height="1072" alt="image" src="https://github.com/user-attachments/assets/38ecbee5-1e7f-41be-9121-4a05ca327374" />
<img width="1920" height="1080" alt="image" src="https://github.com/user-attachments/assets/d619eb09-6868-447e-a5be-e154aa142d30" />
<img width="1920" height="1072" alt="image" src="https://github.com/user-attachments/assets/a89e9d29-0723-4fd1-bede-de948b5f53a8" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/2fd61c26-1c3c-4320-ae06-6d63b4f701ac" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/e77fd36e-8aaa-409c-a561-f9193b038e8e" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/464790de-5cfd-4141-9d1f-f33e7a3c2046" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/bd72f128-05d2-42f7-9436-fa98ddd89e5f" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/594daec1-a38d-4720-a928-5560e9cfcd1f" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/94a13c3a-be21-4178-8acf-14f0b111e460" />
<img width="1920" height="1072" alt="image" src="https://github.com/user-attachments/assets/88ace4ed-d9d8-43bd-9179-0bb12f4d4c9a" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/388f0c6e-f4da-47bd-9547-2f02d8eca504" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/630f20a3-4f91-4d91-addb-d7e4d9c61374" />
Completed succesfully.


## Example: parsing and embedding a 678-page native-text PDF

Start:
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/38fd80e9-621a-428a-a640-a8183e5159a9" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/ae4f7561-7b8c-4004-bdda-cbda9042ed8d" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/cd038291-1823-4068-b0aa-e76dcbe74d47" />
<img width="794" height="962" alt="image" src="https://github.com/user-attachments/assets/871a04ac-c3da-4720-8c3d-020ae53c102b" />

<img width="1920" height="1072" alt="image" src="https://github.com/user-attachments/assets/00037ece-2a25-4e44-837f-986ced685c43" />
<img width="1920" height="1072" alt="image" src="https://github.com/user-attachments/assets/33122e8a-283a-47ef-b4de-3dce923da9c2" />

<img width="897" height="962" alt="image" src="https://github.com/user-attachments/assets/1f158773-c327-467a-b6db-350f44366bf7" />
