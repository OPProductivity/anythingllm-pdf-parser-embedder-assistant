# AnythingLLM PDF Parser Embedder Assistant

**Local PDF-to-text, OCR, page-aware chunking, and AnythingLLM Desktop upload
automation for retrieval-augmented generation (RAG).**

This AnythingLLM (Windows 11) assistant turns one PDF, a selected batch or folder of
PDFs, into clean text files, and uploads them and embeds them to AnythingLLM. AnythingLLM itself does not support
uploading PDF files, and uses .TXT or .JSON. Optionally split files into one or more segments per page and send
the prepared records in a subfolder to your local AnythingLLM Desktop workspace installed on the user's pc. It is designed for people who want
to more easily work with PDFs inside AnythingLLM. You can also create local files only (only parse/perform OCR) to automatically convert PDFs to TXT and then upload to AnythingLLM manually. The advantage is that this software works with existing PDF metadata and bookmarks and can also recognize title pages, content index, bibliography and more (which can be excluded) and can also perform OCR on only a subset (5 out of 20 pages only need OCR).
Doing this automatically or in batch mode is faster than manually creating  workspaces and uploading and embedding pdf files.
Depending on whether you use local ollama embedders, or openrouter embedders via api key, this can be done under half a minute per pdf. 
The app communicates with AnythingLLM settings and workspaces through the official AnythingLLM API key. Be sure to vet the codebase of this app (manually or using AI) to see if it complies with your own data processing protocols, as it will be able to make embedding decisions using local python scripts. The app operates via a localhost structure at port 7860 (http://127.0.0.1:7860/) and is a completely local app. But it uses the AnythingLLM app to send PDFs to local embedders (privacy-friendly) or cloud embedder providers (different data processing policies) using the provided (default) settings and the settings you have made personally in the AnythingLLM app. Start up AnythingLLM before using the app.

AnythingLLM PDF Parser Embedder Assistant supports parsing native-text PDFs, PDFs that need OCR (using Tesseract) for all 
pages or a subset of pages, and can also use the Unstructured library for text extraction 
from tables and documents with complex layout. It can do whole documents, whole-page chunks, automatically page-preserved records, and shorter
page-local passages and can do a custom range of pages or all pages.

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



## Why use it?

- Preserve PDF pages for citing or retrieving directly from page (higher LLM citation accuracy).
- Convert PDFs into text records before embedding.
- Parse PDF files in batches
- Upload your pdf, and use the default settings by simply clicking "Confirm and start processing" at the bottom of the page,
  and let the app create a new workspace for you with the title of the pdf file itself.
- Parse & Upload PDFs in batches at the same time to AnythingLLM (new or existing workspace) without clicking through menus for each PDF
- Choose a segmentation strategy that fits retrieval quality and speed (one file, multiple files)
- Prepare and upload a single PDF, several selected PDFs, or a batch folder.
- Keep parsing and OCR locally when you do not want to upload anything.
- Detect extraction, OCR, embedding, workspace, and retrieval failures
- Parse advanced PDFs using complex python scripts
- Easy localhost user interface - no terminal required
- Dark Theme and Light Theme (also follows System Theme if your windows dark mode only activates at night)
- Adheres to common embedder context size limits
- Use the optional desktop refresh bridge to make AnythingLLM Desktop reflect
  completed document changes more reliably.

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
    Upload --> Verify["Storage, vector, and retrieval checks"]
```

### Output modes

| Mode | Result |
| --- | --- |
| **Create local files only** | Parsed transcript and selected segments with run evidence. |
| **Create local files without logs** | A compact output folder containing only the parsed transcript and text segments. |
| **Create local files and upload to AnythingLLM** | Local output plus workspace upload, embedding queue submission, and post-upload checks. |

### Segmentation choices

- **All in one file:** one text record for the entire PDF.
- **Whole-page chunks:** one record for each extracted page.
- **Page – preserve automatically:** page-addressable records, with page-local
  splitting only where necessary to fit the effective upload boundary.
- **Shorter page-local passages:** smaller page-addressable passages for more
  granular retrieval.

The app reports what it observed. It does not claim that an AnythingLLM document
drawer view alone proves retrieval readiness; storage, vectors, and runtime
retrieval are separate checks.

## Quick start

### Requirements

- Windows 10/11
- Python 3.11–3.14
- [pipx](https://pipx.pypa.io/)
- [AnythingLLM Desktop](https://anythingllm.com/desktop) running locally when
  you use the upload mode
- An embedding provider configured and working in AnythingLLM Desktop (install Ollama on your PC or configure OpenRouter API key inside AnythingLLM)

### Installation

## Install the app directly from this Github Repository to your Windows PC:
```powershell
py -m pip install --user pipx
py -m pipx ensurepath
pipx install git+https://github.com/OPProductivity/anythingllm-pdf-parser-embedder-assistant.git
```

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

For a development checkout, clone the repository and run `pipx install .` in
the repository root instead.

## How to use the app

1. Start AnythingLLM Desktop and confirm that its embedding provider works.
2. Start this assistant with `anythingllm-pdf-assistant start --browser` (or use the desktop shortcut).
3. Upload a PDF, choose several PDFs, or select a batch folder.
4. Review the detected PDF metadata and set title/author information if needed.
5. Select an output mode and a segmentation mode.
6. For upload mode, choose an existing workspace or create a new one.
7. Confirm the settings. The app creates local outputs before changing the
   AnythingLLM workspace.
8. Review the completion state and the generated output folder. For upload
   runs, distinguish local preparation, document storage, vector observation,
   and retrieval evidence in the run report.

For a detailed walkthrough, see [docs/USAGE.md](docs/USAGE.md).

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

Contributions, bug reports, and reproducible test cases are welcome. Read
[CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md) first.

## Related projects and official references

- [AnythingLLM](https://github.com/Mintplex-Labs/anything-llm)
- [AnythingLLM documentation](https://docs.anythingllm.com/)
- AnythingLLM's local developer API documentation at `http://127.0.0.1:3001/api/docs`
- [pipx documentation](https://pipx.pypa.io/)

## License

MIT. See [LICENSE](LICENSE).

## Succesfully Parsing and embedding a PDF with 2 columns of text per page - 6 page pdf - 0 OCR pages - parsed - split into 6 - embedded into AnythingLLM

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


## Succesfully Parsing and embedding a normal PDF - 678 pages pdf - 0 OCR pages - parsed - split into 6 - embedded into AnythingLLM

Start:
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/38fd80e9-621a-428a-a640-a8183e5159a9" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/ae4f7561-7b8c-4004-bdda-cbda9042ed8d" />
<img width="1639" height="980" alt="image" src="https://github.com/user-attachments/assets/cd038291-1823-4068-b0aa-e76dcbe74d47" />
<img width="794" height="962" alt="image" src="https://github.com/user-attachments/assets/871a04ac-c3da-4720-8c3d-020ae53c102b" />

<img width="1920" height="1072" alt="image" src="https://github.com/user-attachments/assets/00037ece-2a25-4e44-837f-986ced685c43" />
<img width="1920" height="1072" alt="image" src="https://github.com/user-attachments/assets/33122e8a-283a-47ef-b4de-3dce923da9c2" />

<img width="897" height="962" alt="image" src="https://github.com/user-attachments/assets/1f158773-c327-467a-b6db-350f44366bf7" />











