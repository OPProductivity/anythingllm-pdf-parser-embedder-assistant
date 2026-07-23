# AnythingLLM PDF Parser Embedder Assistant

Local PDF preparation, page-aware segmentation, and optional upload to a
locally running AnythingLLM Desktop instance.

## Install

Install Python 3.11–3.14 and pipx, then install the application from a local
checkout during development:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
pipx install .
```

For a published Git repository, the equivalent command is:

```powershell
pipx install git+https://github.com/USERNAME/anythingllm-pdf-assistant.git
```

Start the app and open its local URL:

```powershell
anythingllm-pdf-assistant start --browser
```

Use `anythingllm-pdf-assistant doctor` to check writable locations and port
availability, or `anythingllm-pdf-assistant paths` to show the current user's
data directory.

## Local data and credentials

The installed Python package is not used as a data folder. Generated output,
logs, timing observations, and optional local configuration are created under:

`%LOCALAPPDATA%\AnythingLLM PDF Parser Embedder Assistant`

Set `ANYTHINGLLM_PDF_ASSISTANT_HOME` before launching to use another writable
location, for example a portable drive. API keys are intentionally not bundled
or committed. Configure provider credentials in AnythingLLM Desktop itself.

## Optional Desktop refresh bridge

The optional bridge modifies the installed AnythingLLM Desktop archive only
after validating exact supported anchors and first makes a timestamped backup.
Close AnythingLLM Desktop before installing, upgrading, or uninstalling it;
validation is read-only and can be run while Desktop is open.

```powershell
anythingllm-pdf-assistant bridge validate
anythingllm-pdf-assistant bridge install
anythingllm-pdf-assistant bridge uninstall
```

The installer automatically locates the normal Desktop resources directory;
use `--resources-path` only for a non-standard installation.

## Before public release

Choose and add a license appropriate for the intended release. Do not commit
`.env` files, AnythingLLM Desktop storage, run outputs, logs, or provider keys.
