# Preserve and run a frozen source snapshot on another local port

The former 7861 installation used a separate copy of the assistant source,
a separate application data directory, and an explicit localhost port. Its
Start and Stop shortcuts selected that source and data directory together.
Changes in the development checkout therefore did not change the snapshot's
Python source. Both installations still used the same AnythingLLM Desktop
backend and, in this installation, the same Python virtual environment.

The original deployment was established on 2026-10-15. The contemporaneous
metadata-population report identifies its early source pin as `302f169`.
Later snapshots were deliberately refreshed to qualified commits. Its last
deployed source was `f3df5359f92e78a33f566ab242194d3a1909cefe` before retirement
on 2026-10-24. The original deployment tree, provenance files, launchers,
shortcuts, and setup-related local reports were archived during retirement.
They remain local historical evidence; this guide describes how to recreate
the arrangement.

## Create a snapshot from a reviewed commit

Choose a qualified commit before making risky edits. `git archive` includes
the files tracked at that commit. It does not include uncommitted changes,
ignored settings, the virtual environment, or the repository's `.git` data.

Use new absolute paths for the snapshot and its data. The following PowerShell
example assumes those paths do not already exist:

```powershell
$repo = 'C:\path\to\assistant-repository'
$base = 'C:\path\to\assistant-snapshot'
$python = Join-Path $repo '.venv\Scripts\python.exe'
$commit = 'FULL_REVIEWED_COMMIT_SHA'
$short = $commit.Substring(0, 7)
$source = Join-Path $base "code-$short"
$archive = Join-Path $base "code-$short.zip"
$data = Join-Path $base 'data'

if (Test-Path -LiteralPath $base) { throw 'Choose a new snapshot directory.' }
New-Item -ItemType Directory -Path $base | Out-Null
Push-Location -LiteralPath $repo
try {
    git archive --format=zip --output=$archive $commit
    if ($LASTEXITCODE -ne 0) { throw 'git archive failed' }
} finally {
    Pop-Location
}
Expand-Archive -LiteralPath $archive -DestinationPath $source
if (-not (Test-Path -LiteralPath (Join-Path $source 'anythingllm_pdf_assistant_cli.py'))) {
    throw 'The snapshot lacks the assistant entry point.'
}
[System.IO.File]::WriteAllText((Join-Path $base 'SOURCE-COMMIT.txt'), $commit)
New-Item -ItemType Directory -Path (Join-Path $data 'logs') -Force | Out-Null
```

Compare hashes of the main modules against the chosen commit or checkout.
Record the full commit, Python version, package versions, source path, data
path, and port in a README next to the archive. Keep the archive and expanded
source together so the snapshot can be reconstructed without a Git checkout.

The historical deployment shared the main checkout's virtual environment.
That freezes the source only. For experiments that change Python packages,
create and qualify a separate virtual environment too. A separate application
data directory does not create a separate AnythingLLM embedding queue or
backend, and backend configuration changes affect both clients.

## Start with an isolated data directory

Save a launcher containing the selected absolute paths and port. The Start
shortcut should invoke that launcher. Launch background helpers with a hidden
window. Set the data environment variable before starting the child and restore
the parent environment afterwards:

```powershell
$port = 7861
$previousData = $env:ANYTHINGLLM_PDF_ASSISTANT_HOME
try {
    $env:ANYTHINGLLM_PDF_ASSISTANT_HOME = $data
    $launchId = '{0}-{1}' -f (Get-Date -Format 'yyyyMMdd-HHmmss-fff'), $PID
    Start-Process -FilePath $python `
        -ArgumentList @('-m', 'anythingllm_pdf_assistant_cli', 'start', '--port', "$port", '--browser') `
        -WorkingDirectory $source -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $data "logs\server-$launchId.stdout.log") `
        -RedirectStandardError (Join-Path $data "logs\server-$launchId.stderr.log")
} finally {
    $env:ANYTHINGLLM_PDF_ASSISTANT_HOME = $previousData
}
```

The CLI writes an ownership marker under this data directory, claims the port,
and opens an authenticated local browser session. Use that launcher to open
the application. The bare localhost URL does not grant a new browser access.
Do not print or copy browser keys, tickets, or provider credentials into a
README. An existing owned server is reused by another Start action.

Check `/healthz`, verify that the listener belongs to the launched process,
and confirm its working directory and source pin. Use a small actual PDF
preparation/upload test when the snapshot is meant to remain usable during
development. A healthy listener alone does not qualify metadata, upload,
or embedding behavior.

## Stop and retire the snapshot

The Stop launcher must use the same source and data paths as Start. The stop
command reads the corresponding ownership marker; changing only the port or
working directory can select the wrong installation's state.

```powershell
$previousData = $env:ANYTHINGLLM_PDF_ASSISTANT_HOME
try {
    $env:ANYTHINGLLM_PDF_ASSISTANT_HOME = $data
    Push-Location -LiteralPath $source
    try {
        & $python -m anythingllm_pdf_assistant_cli stop
        if ($LASTEXITCODE -ne 0) { throw 'Owned snapshot stop failed' }
    } finally {
        Pop-Location
    }
} finally {
    $env:ANYTHINGLLM_PDF_ASSISTANT_HOME = $previousData
}
```

Finish or deliberately cancel active processing before retiring it. Verify
that its listener and owned process have stopped. Archive the deployment,
README, SOURCE-COMMIT file, Start/Stop scripts and shortcuts, and relevant
qualification records. Keep application data and run evidence separate from
the normal installation. Remove the active shortcuts only after preserving
their targets and arguments. Retiring the second assistant does not remove
AnythingLLM workspaces or vectors stored in the shared backend.

## Metadata override upload qualification on 2026-10-24

Two controlled one-page PDFs were exercised through each actual server's browser
and default native upload path. Each used a newly created AnythingLLM workspace:

| Port | Override inputs | Expected persisted metadata |
| --- | --- | --- |
| 7860 | All four filled | Verified Work 7860; Helena Iris Hartwell; Hartwell7860; Verified Academic Press 7860 |
| 7860 | All four blank | Woodland Archive Practice; Clara Isabel Linden; Linden; Woodland Research Press |
| 7861 | All four filled | Verified Work 7861; Helena Iris Hartwell; Hartwell7861; Verified Academic Press 7861 |
| 7861 | All four blank | Woodland Archive Practice; Clara Isabel Linden; Linden; Woodland Research Press |

Verification reads the saved raw document JSON, the workspace's SQLite document
metadata, its document/vector mapping, and the workspace's actual LanceDB row.
The author is stored in `docAuthor`. The source title and Publisher are stored
in `description`; the citation label forms the page record's stored title and
filename. Publisher has no separate native AnythingLLM field in this upload
contract. The original PDF text retains its original byline even when the
metadata author is deliberately overridden.

All four cases passed. The first three records were freshly embedded. The
second upload of the unchanged blank-override PDF on 7861 used AnythingLLM's
normal cache linkage; its stored vector metadata was checked as well. Each
workspace contained one confirmed 4,096-dimensional vector, and each retained
integrity audit reported no errors or warnings.

These controlled inputs test override persistence and blank-field fallback.
They do not establish corpus recognition accuracy or retrieval-answer quality.
No runtime benchmark is part of this qualification.
