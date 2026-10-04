[CmdletBinding()]
param(
    [Alias("PackageUrl")][string]$BundleUrl = "",
    [string]$BundlePath = "",
    [switch]$VerifyOnly,
    [Parameter(Mandatory = $true)][ValidatePattern('^[0-9a-fA-F]{64}$')][string]$BundleSha256
)

$ErrorActionPreference = "Stop"
if ((-not $BundleUrl) -eq (-not $BundlePath)) {
    throw "Specify exactly one verified release BundleUrl or BundlePath. Mutable main-branch source installation is no longer allowed."
}
if ($BundleUrl -and ([Uri]$BundleUrl).Scheme -ne "https") {
    throw "Release downloads require HTTPS."
}
$releaseScratch = Join-Path ([IO.Path]::GetTempPath()) ("pdf-assistant-release-" + [guid]::NewGuid().ToString("N"))
function Remove-OwnedReleaseScratch {
    $target = [IO.Path]::GetFullPath($script:releaseScratch)
    $root = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if (-not $target.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { throw "Refusing release cleanup outside Temp." }
    if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
}
New-Item -ItemType Directory -Path $releaseScratch | Out-Null
$archive = Join-Path $releaseScratch "release.zip"
try {
    if ($BundleUrl) { Invoke-WebRequest -Uri $BundleUrl -OutFile $archive }
    else { Copy-Item -LiteralPath $BundlePath -Destination $archive }
    if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $BundleSha256) {
        throw "Release hash mismatch. No package installer or release code was executed."
    }
    $releaseFiles = Join-Path $releaseScratch "verified"
    Expand-Archive -LiteralPath $archive -DestinationPath $releaseFiles
    $manifest = Get-Content -LiteralPath (Join-Path $releaseFiles "release-manifest.json") -Raw | ConvertFrom-Json
    if ($manifest.schema_version -ne 1 -or $manifest.kind -ne "pdf-assistant-wheels") {
        throw "This is not a supported verified wheel release."
    }
    foreach ($file in $manifest.files) {
        $root = [IO.Path]::GetFullPath($releaseFiles) + [IO.Path]::DirectorySeparatorChar
        $candidate = [IO.Path]::GetFullPath((Join-Path $releaseFiles $file.path))
        if (-not $candidate.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { throw "Invalid release path." }
        if ((Get-FileHash -LiteralPath $candidate -Algorithm SHA256).Hash -ne $file.sha256) { throw "Release member hash mismatch." }
    }
} catch {
    # Only this randomly reserved scratch tree is removed.
    Remove-OwnedReleaseScratch
    throw
}

if ($VerifyOnly) {
    Write-Host "Release archive and all manifest members verified. No release code was executed."
    Remove-OwnedReleaseScratch
    exit 0
}

try {

function Find-SupportedPython {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if (-not $launcher) {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
    }
    if ($launcher) {
        foreach ($version in @($manifest.python_version)) {
            $executable = (& $launcher.Source ("-" + $version) -c "import sys; print(sys.executable)" 2>$null).Trim()
            if ($LASTEXITCODE -eq 0 -and $executable) {
                return @{ Command = $launcher.Source; Selector = @( "-" + $version ); Version = $version; Executable = $executable }
            }
        }
    }

    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $python) {
        $python = Get-Command python -ErrorAction SilentlyContinue
    }
    if ($python) {
        $version = (& $python.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null).Trim()
        if ($LASTEXITCODE -eq 0 -and $version -eq $manifest.python_version) {
            return @{ Command = $python.Source; Selector = @(); Version = $version; Executable = $python.Source }
        }
    }
    return $null
}

function Test-TesseractInstalled {
    if ((Get-Command tesseract.exe -ErrorAction SilentlyContinue) -or (Get-Command tesseract -ErrorAction SilentlyContinue)) {
        return $true
    }
    $programFiles = [Environment]::GetFolderPath("ProgramFiles")
    $programFilesX86 = ${env:ProgramFiles(x86)}
    $localAppData = [Environment]::GetFolderPath("LocalApplicationData")
    foreach ($candidate in @(
        (Join-Path $programFiles "Tesseract-OCR\\tesseract.exe"),
        (Join-Path $programFilesX86 "Tesseract-OCR\\tesseract.exe"),
        (Join-Path $localAppData "Programs\\Tesseract-OCR\\tesseract.exe")
    )) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $true
        }
    }
    return $false
}

function Test-AnythingLLMDesktopInstalled {
    $localAppData = [Environment]::GetFolderPath("LocalApplicationData")
    $programFiles = [Environment]::GetFolderPath("ProgramFiles")
    $programFilesX86 = ${env:ProgramFiles(x86)}
    foreach ($candidate in @(
        (Join-Path $localAppData "Programs\\AnythingLLM\\AnythingLLM.exe"),
        (Join-Path $programFiles "AnythingLLM\\AnythingLLM.exe"),
        (Join-Path $programFilesX86 "AnythingLLM\\AnythingLLM.exe")
    )) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return $true
        }
    }
    return $false
}

function Offer-OfficialSetupPage {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Purpose,
        [Parameter(Mandatory = $true)][string]$Url
    )

    Write-Warning "$Name was not detected. $Purpose"
    $approval = Read-Host "Open the official $Name installation page now? [y/N]"
    if ($approval -match "^(?i:y|yes)$") {
        Start-Process $Url
    } else {
        Write-Host "Skipped. You can install $Name later from: $Url"
    }
}

$pythonInstallation = Find-SupportedPython
if (-not $pythonInstallation) {
    $requestedPython = $manifest.python_version
    if ($requestedPython -notin @("3.11", "3.12", "3.13", "3.14")) { throw "Unsupported release Python version." }
    $approval = Read-Host "Python $requestedPython is required for this release. Install it for this user with winget now? [y/N]"
    if ($approval -notmatch "^(?i:y|yes)$") {
        throw "A supported Python installation is required. No Python installation was started."
    }
    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $winget) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
    }
    if (-not $winget) {
        throw "winget is not available. Install Python 3.11 through 3.14 yourself, then run this installer again."
    }
    & $winget.Source install --id "Python.Python.$requestedPython" --exact --scope user
    if ($LASTEXITCODE -ne 0) {
        throw "winget could not install Python $requestedPython. No further installation steps were run."
    }
    $pythonInstallation = Find-SupportedPython
    if (-not $pythonInstallation) {
        throw "Python $requestedPython was installed but is not yet visible to the Python launcher. Close and reopen PowerShell, then run this installer again."
    }
}

$pythonLauncherPath = $pythonInstallation.Command
$pythonSelector = @($pythonInstallation.Selector)
Write-Host "Using Python $($pythonInstallation.Version): $($pythonInstallation.Executable)"

function Invoke-Python {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    $invocation = @($script:pythonSelector) + @($Arguments)
    & $script:pythonLauncherPath @invocation
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed: py $($Arguments -join ' ')"
    }
}

if ($manifest.python_version -ne $pythonInstallation.Version -or $manifest.platform -ne "win_amd64") {
    throw "This verified bundle does not match Python $($pythonInstallation.Version) on Windows x64. Use the matching release bundle."
}
$runtime = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) ("Programs\AnythingLLM PDF Assistant\runtimes\" + $BundleSha256.Substring(0, 16) + "-py" + $pythonInstallation.Version)
if (Test-Path -LiteralPath $runtime) {
    throw "This release runtime already exists. Refusing to modify a potentially running installation: $runtime"
}
Invoke-Python -Arguments @("-m", "venv", $runtime)
$assistantPython = Join-Path $runtime "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $assistantPython -PathType Leaf)) {
    throw "The release Python executable was not found at $assistantPython."
}
Push-Location $releaseFiles
try {
    & $assistantPython -m pip install --isolated --no-index --no-deps --require-hashes --force-reinstall -r requirements-release.lock
    if ($LASTEXITCODE -ne 0) { throw "Verified offline wheel installation failed." }
    & $assistantPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Verified release dependency graph is incomplete or incompatible." }
} finally {
    Pop-Location
    Remove-OwnedReleaseScratch
}

& $assistantPython -m anythingllm_pdf_assistant_cli shortcuts repair
if ($LASTEXITCODE -ne 0) {
    throw "The assistant installed, but its desktop shortcuts could not be created. Run `anythingllm-pdf-assistant shortcuts repair` after resolving the reported error."
}

Write-Host "Installation complete. Start and Stop shortcuts were created on your Desktop."
Write-Host "First-use tip: if the app's options feel overwhelming, choose your PDF and output mode first, then scroll down to the blue Confirm and start processing button. The remaining options can stay at their defaults for your first run."

if (-not (Test-AnythingLLMDesktopInstalled)) {
    Offer-OfficialSetupPage -Name "AnythingLLM Desktop" -Purpose "It is required to upload prepared records and create embeddings; local PDF extraction can still be used without it." -Url "https://anythingllm.com/desktop"
}
if (-not (Test-TesseractInstalled)) {
    Offer-OfficialSetupPage -Name "Tesseract OCR" -Purpose "It is required for scanned or image-only PDFs and the Unstructured hi_res/ocr_only extraction modes." -Url "https://tesseract-ocr.github.io/tessdoc/Installation.html"
}
} finally {
    Remove-OwnedReleaseScratch
}
