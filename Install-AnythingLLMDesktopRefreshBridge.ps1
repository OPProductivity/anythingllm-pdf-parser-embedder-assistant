[CmdletBinding()]
param(
    # Optional explicit installation path. When omitted, the installer checks
    # the standard per-user and machine-wide AnythingLLM Desktop locations.
    [string]$ResourcesPath = "",
    [switch]$Uninstall,
    [switch]$Upgrade,
    # Read and report the installed Desktop version, bridge status, and exact
    # supported startup-anchor profile without changing AnythingLLM files.
    [switch]$Validate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-BridgeModuleSource {
@'
"use strict";

// This module is deliberately narrow. It exposes one authenticated, loopback-only
// operation: safely reload the rendered AnythingLLM workspace sidebar.
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const path = require("path");

const BRIDGE_MARKER = "anythingllm-pdf-prep-refresh-bridge-v1";
const BRIDGE_REVISION = "drawer-audit-v2";
const BRIDGE_FILE_NAME = "anythingllm-pdf-prep-refresh-bridge.json";
const DRAFT_GUARD_VERSION = 2;
const REFRESH_SCRIPT = 'window.dispatchEvent(new Event("refresh-workspaces")); ({ refreshed: true })';
const DRAFT_CHECK_SCRIPT = `(() => {
  try {
    const selector = [
      'textarea',
      'input:not([type])',
      'input[type="text"]',
      'input[type="email"]',
      'input[type="url"]',
      'input[type="tel"]',
      'input[type="search"]',
      '[contenteditable]',
      '[role="textbox"]',
    ].join(', ');
    const fields = Array.from(document.querySelectorAll(selector));
    const visibleAndEditable = (field) => {
      const style = window.getComputedStyle(field);
      return !!style
        && style.display !== 'none'
        && style.visibility !== 'hidden'
        && field.getClientRects().length > 0
        && !field.disabled
        && !field.readOnly;
    };
    const draft = fields.some((field) => {
      if (!visibleAndEditable(field)) return false;
      const value = typeof field.value === 'string'
        ? field.value
        : (field.innerText || field.textContent || '');
      return String(value || '').trim().length > 0;
    });
    return { safeToReload: !draft, inspectedFields: fields.length };
  } catch (_) {
    // A draft guard must fail closed: an unknown renderer shape cannot justify
    // dropping a user's in-progress message for a sidebar refresh.
    return { safeToReload: false, inspectedFields: 0, inspectionFailed: true };
  }
})()`;

function isLoopback(address) {
  return address === "127.0.0.1" || address === "::1" || address === "::ffff:127.0.0.1";
}

function tokenMatches(provided, expected) {
  if (typeof provided !== "string") return false;
  const actual = Buffer.from(provided, "utf8");
  const wanted = Buffer.from(expected, "utf8");
  return actual.length === wanted.length && crypto.timingSafeEqual(actual, wanted);
}

function writeJson(response, status, body) {
  response.writeHead(status, { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" });
  response.end(JSON.stringify(body));
}

function startPdfPrepRefreshBridge({ app, getMainWindow }) {
  if (!app || typeof app.getPath !== "function") return null;

  const token = crypto.randomBytes(32).toString("base64url");
  // Electron may prune transient files directly under userData while it is
  // starting. AnythingLLM's durable storage directory survives that cleanup.
  const configPath = path.join(app.getPath("userData"), "storage", BRIDGE_FILE_NAME);
  const diagnosticPath = path.join(app.getPath("userData"), "pdf-prep-refresh-bridge.log");
  let server = null;
  let descriptorInterval = null;
  let cleaned = false;

  const diagnostic = (event, details = "") => {
    try { fs.appendFileSync(diagnosticPath, `${new Date().toISOString()} ${event}${details ? ` ${details}` : ""}\n`, { encoding: "utf8" }); } catch (_) {}
  };

  const cleanup = () => {
    if (cleaned) return;
    cleaned = true;
    diagnostic("cleanup");
    try { if (descriptorInterval) clearInterval(descriptorInterval); } catch (_) {}
    try { if (server) server.close(); } catch (_) {}
    try { fs.rmSync(configPath, { force: true }); } catch (_) {}
  };

  const reloadRendererAndWait = (webContents, timeoutMs = 5000) => new Promise((resolve) => {
    let settled = false;
    let timer = null;
    const finish = (rendererReady) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      try { webContents.removeListener("did-finish-load", onFinished); } catch (_) {}
      resolve({ rendererReady });
    };
    const onFinished = () => finish(true);
    webContents.once("did-finish-load", onFinished);
    timer = setTimeout(() => finish(false), timeoutMs);
    try {
      webContents.reloadIgnoringCache();
    } catch (_) {
      finish(false);
    }
  });

  server = http.createServer(async (request, response) => {
    request.resume();
    if (!isLoopback(request.socket.remoteAddress)) {
      diagnostic("request_rejected", "non_loopback");
      return writeJson(response, 403, { ok: false, error: "loopback_only" });
    }
    if (request.method !== "POST" || request.url !== "/v1/refresh-workspaces") {
      diagnostic("request_rejected", "not_found");
      return writeJson(response, 404, { ok: false, error: "not_found" });
    }
    if (!tokenMatches(request.headers["x-anythingllm-pdf-prep-bridge"], token)) {
      diagnostic("request_rejected", "unauthorized");
      return writeJson(response, 401, { ok: false, error: "unauthorized" });
    }

    diagnostic("refresh_requested");

    const window = getMainWindow && getMainWindow();
    if (!window || window.isDestroyed() || !window.webContents || window.webContents.isDestroyed()) {
      diagnostic("refresh_failed", "desktop_window_unavailable");
      return writeJson(response, 503, { ok: false, error: "desktop_window_unavailable" });
    }

    try {
      const draftState = await window.webContents.executeJavaScript(DRAFT_CHECK_SCRIPT, true);
      if (!draftState || !draftState.safeToReload) {
        diagnostic("refresh_deferred", "unsent_draft_detected");
        return writeJson(response, 409, {
          ok: false,
          error: "unsent_draft_detected",
          action: "refresh_deferred_without_renderer_event_or_reload",
        });
      }
      await window.webContents.executeJavaScript(REFRESH_SCRIPT, true);
      const reload = await reloadRendererAndWait(window.webContents);
      diagnostic("refresh_completed", reload.rendererReady ? "renderer_ready" : "renderer_reload_started");
      return writeJson(response, 202, {
        ok: true,
        action: reload.rendererReady ? "renderer_ready_after_workspace_refresh" : "renderer_reload_started_after_workspace_refresh",
        rendererReady: reload.rendererReady,
        refreshedAt: new Date().toISOString(),
      });
    } catch (error) {
      diagnostic("refresh_failed", "renderer_refresh_failed");
      return writeJson(response, 503, { ok: false, error: "renderer_refresh_failed" });
    }
  });

  server.once("error", (error) => {
    console.error("[PDF Prep refresh bridge] failed to start", error && error.message ? error.message : error);
    diagnostic("server_error", error && error.code ? error.code : "unknown");
    cleanup();
  });
  server.listen(0, "127.0.0.1", () => {
    const address = server.address();
    if (!address || typeof address === "string") return cleanup();
    const descriptor = {
      marker: BRIDGE_MARKER,
      schemaVersion: 1,
      draftGuardVersion: DRAFT_GUARD_VERSION,
      pid: process.pid,
      port: address.port,
    token,
    appVersion: app.getVersion(),
    bridgeRevision: BRIDGE_REVISION,
    startedAt: new Date().toISOString(),
    };
    const publishDescriptor = () => {
      if (!server || !server.listening) return;
      try {
        fs.writeFileSync(configPath, JSON.stringify(descriptor), { encoding: "utf8", mode: 0o600 });
        try { fs.chmodSync(configPath, 0o600); } catch (_) {}
      } catch (error) {
        diagnostic("descriptor_write_error", error && error.code ? error.code : "unknown");
      }
    };
    try {
      publishDescriptor();
      diagnostic("listening", `port=${address.port}`);
      // Some Desktop startup paths clear unknown user-data files after the
      // main process is already live. Re-publish this ephemeral descriptor so
      // its visibility accurately tracks the running bridge rather than that
      // one-time cleanup race. The timer is unref'd and cannot keep Desktop
      // alive or alter any UI state.
      descriptorInterval = setInterval(publishDescriptor, 1000);
      descriptorInterval.unref();
    } catch (error) {
      console.error("[PDF Prep refresh bridge] failed to write descriptor", error && error.message ? error.message : error);
      cleanup();
    }
  });
  server.unref();
  // `before-quit` is also emitted by some Desktop restart/cleanup paths while
  // the main process continues. `will-quit` is the true final lifecycle event,
  // so it keeps the descriptor available for the whole live Desktop session.
  app.once("will-quit", cleanup);
  // Some process exits do not traverse Electron's app lifecycle. This sync-only
  // fallback removes the capability descriptor even then; it never keeps the
  // app alive and has no UI side effect.
  process.once("exit", cleanup);
  return { marker: BRIDGE_MARKER, configPath };
}

module.exports = { startPdfPrepRefreshBridge };
'@
}

function Get-NodeRunner {
    $npx = Get-Command npx.cmd -ErrorAction SilentlyContinue
    if (-not $npx) { $npx = Get-Command npx -ErrorAction SilentlyContinue }
    if (-not $npx) {
        throw "npx was not found. Install Node.js, then rerun this script."
    }
    return $npx.Source
}

function Assert-AnythingLLMIsClosed {
    $processes = @(Get-Process -Name "AnythingLLM" -ErrorAction SilentlyContinue)
    if ($processes.Count -gt 0) {
        throw "AnythingLLM is running. Close it normally, then rerun this script. The installer will not terminate it or automate its UI."
    }
}

function Resolve-AnythingLLMResourcesPath {
    param([string]$RequestedPath)

    if (-not [string]::IsNullOrWhiteSpace($RequestedPath)) {
        if (-not (Test-Path -LiteralPath $RequestedPath -PathType Container)) {
            throw "The requested AnythingLLM resources path does not exist: $RequestedPath"
        }
        return (Resolve-Path -LiteralPath $RequestedPath).Path
    }

    $candidates = @(
        (Join-Path $env:LOCALAPPDATA "Programs\AnythingLLM\resources"),
        (Join-Path $env:LOCALAPPDATA "Programs\AnythingLLM Desktop\resources"),
        (Join-Path $env:ProgramFiles "AnythingLLM\resources"),
        (Join-Path ${env:ProgramFiles(x86)} "AnythingLLM\resources")
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    $match = @($candidates | Where-Object {
        Test-Path -LiteralPath (Join-Path $_ "app.asar") -PathType Leaf
    } | Select-Object -First 1)
    if ($match.Count -ne 1) {
        throw "AnythingLLM Desktop resources were not found automatically. Re-run with -ResourcesPath '<installation>\\resources>'."
    }
    return (Resolve-Path -LiteralPath $match[0]).Path
}

$resources = Resolve-AnythingLLMResourcesPath -RequestedPath $ResourcesPath
$asarPath = Join-Path $resources "app.asar"
if (-not (Test-Path -LiteralPath $asarPath)) {
    throw "Could not find app.asar at $asarPath"
}

if ($Validate -and ($Uninstall -or $Upgrade)) {
    throw "-Validate is read-only and cannot be combined with -Uninstall or -Upgrade."
}

# Validation only reads the archive, so it remains useful while Desktop is
# running. Every operation that writes or restores app.asar still requires a
# normally closed Desktop process.
if (-not $Validate) {
    Assert-AnythingLLMIsClosed
}

if ($Uninstall) {
    $backups = @(Get-ChildItem -LiteralPath $resources -Filter "app.asar.pdf-prep-refresh-bridge-backup-*" -File | Sort-Object LastWriteTime -Descending)
    if ($backups.Count -eq 0) {
        throw "No PDF Prep refresh bridge backup was found in $resources"
    }
    Copy-Item -LiteralPath $backups[0].FullName -Destination $asarPath -Force
    Write-Host "Restored $($backups[0].Name). Start AnythingLLM normally to use the unmodified Desktop app."
    exit 0
}

$npx = Get-NodeRunner
$work = Join-Path ([System.IO.Path]::GetTempPath()) ("anythingllm-pdf-prep-bridge-" + [guid]::NewGuid().ToString("N"))
$backupPath = Join-Path $resources ("app.asar.pdf-prep-refresh-bridge-backup-" + (Get-Date -Format "yyyyMMdd-HHmmss"))

try {
    & $npx --yes @electron/asar extract $asarPath $work
    if ($LASTEXITCODE -ne 0) { throw "Could not extract AnythingLLM app.asar." }

    $packagePath = Join-Path $work "package.json"
    $version = (Get-Content -LiteralPath $packagePath -Raw | ConvertFrom-Json).version
    try {
        # AnythingLLM Desktop appends release labels such as ``-r2`` to an
        # otherwise normal semantic version.  PowerShell's [version] rejects
        # that label, which used to make the bridge installer refuse a newer
        # supported Desktop build before it could even check its safe anchors.
        # Compare the numeric release core only; the exact code shape remains
        # protected by the anchors below.
        $versionText = [string]$version
        $versionMatch = [regex]::Match($versionText, '^[vV]?(\d+\.\d+\.\d+)(?:[-+].*)?$')
        if (-not $versionMatch.Success) {
            throw "Unsupported version format"
        }
        $parsedVersion = [version]$versionMatch.Groups[1].Value
    }
    catch {
        throw "AnythingLLM Desktop reported an invalid version '$version'. No files were changed."
    }
    $minimumSupportedVersion = [version]"1.14.2"
    if ($parsedVersion -lt $minimumSupportedVersion) {
        throw "This guarded bridge supports AnythingLLM Desktop $minimumSupportedVersion and later, but found $version. No files were changed."
    }

    $mainPath = Join-Path $work "dist-electron\main\index.js"
    $bridgePath = Join-Path $work "dist-electron\main\pdf-prep-refresh-bridge.cjs"
    $main = Get-Content -LiteralPath $mainPath -Raw
    $alreadyInstalled = $main.Contains("pdf-prep-refresh-bridge.cjs")
    $bridgeModulePresent = Test-Path -LiteralPath $bridgePath
    $bridgeRevision = ""
    $bridgeDiagnosticsPresent = $false
    if ($bridgeModulePresent) {
        $installedBridgeSource = Get-Content -LiteralPath $bridgePath -Raw
        $revisionMatch = [regex]::Match($installedBridgeSource, 'BRIDGE_REVISION\s*=\s*["''](?<revision>[^"'']+)["'']')
        if ($revisionMatch.Success) {
            $bridgeRevision = $revisionMatch.Groups['revision'].Value
        }
        $bridgeDiagnosticsPresent = $installedBridgeSource -match 'diagnostic\("refresh_requested"'
    }
    # Each profile has two exact, one-occurrence anchors.  Version acceptance
    # alone is deliberately insufficient: the bridge must fail closed instead
    # of guessing through minified Electron startup code after an update.
    $legacyImportAnchor = 'const QZ=Ee.join(__dirname,"../preload/index.js");'
    $legacyStartAnchor = '}}),Wn(he.app,vZ);const t=process.env.VITE_DEV_SERVER_URL||null;'
    $r2ImportAnchor = 'const ZX=Ee.join(__dirname,"../preload/index.js");'
    $r2StartAnchor = 'he.app.whenReady().then(XX).catch(e=>{'

    # An AnythingLLM release label is not a compatibility guarantee.  The
    # profile below first narrows the intended release family, then demands
    # two exact one-occurrence code seams before installing.  That means a
    # future rN build is accepted only when its actual Electron startup shape
    # is still the one we independently verified for 1.15.
    $legacyAnchorMatch = (
        [regex]::Matches($main, [regex]::Escape($legacyImportAnchor)).Count -eq 1 -and
        [regex]::Matches($main, [regex]::Escape($legacyStartAnchor)).Count -eq 1
    )
    $r2AnchorMatch = (
        $versionText -match '^[vV]?1\.15\.0(?:-r\d+)?$' -and
        [regex]::Matches($main, [regex]::Escape($r2ImportAnchor)).Count -eq 1 -and
        [regex]::Matches($main, [regex]::Escape($r2StartAnchor)).Count -eq 1
    )
    $patchedR2AnchorMatch = (
        $versionText -match '^[vV]?1\.15\.0(?:-r\d+)?$' -and
        $bridgeModulePresent -and
        $main.Contains('const __pdfPrepRefreshBridge=require("./pdf-prep-refresh-bridge.cjs");' + $r2ImportAnchor) -and
        $main.Contains('he.app.whenReady().then(XX).then(()=>__pdfPrepRefreshBridge.startPdfPrepRefreshBridge({app:he.app,getMainWindow:()=>$X})).catch(e=>{')
    )
    $anchorProfile = if ($legacyAnchorMatch) {
        "legacy-main-window-v1"
    }
    elseif ($r2AnchorMatch) {
        "anythingllm-1.15-main-window-x"
    }
    elseif ($patchedR2AnchorMatch) {
        "anythingllm-1.15-main-window-x-installed"
    }
    else {
        "unsupported"
    }

    if ($Validate) {
        [pscustomobject]@{
            DesktopVersion = $versionText
            BridgeInstalled = $alreadyInstalled
            BridgeModulePresent = $bridgeModulePresent
            BridgeRevision = $bridgeRevision
            BridgeDiagnosticsPresent = $bridgeDiagnosticsPresent
            CurrentBridgeRevision = ($bridgeRevision -eq "drawer-audit-v2")
            SafeAnchorProfile = $anchorProfile
            CanInstallOrUpgrade = (($alreadyInstalled -and $bridgeModulePresent) -or $anchorProfile -ne "unsupported")
            AppAsar = $asarPath
        }
        exit 0
    }

    if ($alreadyInstalled -and -not $Upgrade) {
        Write-Host "The PDF Prep Desktop refresh bridge is already installed. No files were changed."
        exit 0
    }

    if (-not $alreadyInstalled) {
        $importAnchor = ""
        $startAnchor = ""
        $startReplacement = ""
        if ($legacyAnchorMatch) {
            $importAnchor = $legacyImportAnchor
            $startAnchor = $legacyStartAnchor
            $startReplacement = '}}),__pdfPrepRefreshBridge.startPdfPrepRefreshBridge({app:he.app,getMainWindow:()=>vZ}),Wn(he.app,vZ);const t=process.env.VITE_DEV_SERVER_URL||null;'
        }
        elseif ($r2AnchorMatch) {
            # 1.15.0-r2 renamed the minified window variables. Waiting for
            # XX to resolve guarantees that $X is the newly-created main
            # BrowserWindow before the bridge can ever service a request.
            $importAnchor = $r2ImportAnchor
            $startAnchor = $r2StartAnchor
            $startReplacement = 'he.app.whenReady().then(XX).then(()=>__pdfPrepRefreshBridge.startPdfPrepRefreshBridge({app:he.app,getMainWindow:()=>$X})).catch(e=>{'
        }
        else {
            throw "AnythingLLM Desktop $version did not match an explicitly verified safe bridge anchor set. No files were changed."
        }
        $main = $main.Replace($importAnchor, 'const __pdfPrepRefreshBridge=require("./pdf-prep-refresh-bridge.cjs");' + $importAnchor)
        $main = $main.Replace($startAnchor, $startReplacement)
        # ``utf8NoBOM`` exists in PowerShell 7 but not Windows PowerShell
        # 5.1, which is the interpreter used by the installed desktop
        # shortcut. Use the .NET encoding directly so the packaged JavaScript
        # remains byte-safe under either host.
        $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::WriteAllText($mainPath, $main, $utf8WithoutBom)
    }
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($bridgePath, (Get-BridgeModuleSource), $utf8WithoutBom)

    Copy-Item -LiteralPath $asarPath -Destination $backupPath -ErrorAction Stop
    & $npx --yes @electron/asar pack $work $asarPath
    if ($LASTEXITCODE -ne 0) { throw "Could not repack AnythingLLM app.asar." }
    # Do not report success merely because asar returned. Confirm the patched
    # capability module made it into the final archive before retaining it.
    $packedEntries = @(& $npx --yes @electron/asar list $asarPath)
    $packedBridgeEntries = @($packedEntries | Where-Object {
        ($_ -replace '\\', '/') -eq '/dist-electron/main/pdf-prep-refresh-bridge.cjs'
    })
    if ($LASTEXITCODE -ne 0 -or $packedBridgeEntries.Count -ne 1) {
        throw "AnythingLLM app.asar was repacked but the refresh bridge module could not be verified. The original archive will be restored."
    }

    Write-Host $(if ($alreadyInstalled) { "Upgraded the PDF Prep Desktop refresh bridge." } else { "Installed the PDF Prep Desktop refresh bridge." })
    Write-Host "Backup: $backupPath"
    Write-Host "Start AnythingLLM normally. The bridge descriptor will be created under %APPDATA%\anythingllm-desktop only while Desktop is running."
}
catch {
    if (Test-Path -LiteralPath $backupPath) {
        Copy-Item -LiteralPath $backupPath -Destination $asarPath -Force
    }
    throw
}
finally {
    if (Test-Path -LiteralPath $work) {
        Remove-Item -LiteralPath $work -Recurse -Force
    }
}
