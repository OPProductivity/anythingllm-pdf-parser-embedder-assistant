[CmdletBinding()]
param(
    [string]$RepositoryUrl = "https://github.com/OPProductivity/anythingllm-pdf-parser-embedder-assistant.git"
)

$ErrorActionPreference = "Stop"

function Find-SupportedPython {
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if (-not $launcher) {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
    }
    if ($launcher) {
        foreach ($version in @("3.14", "3.13", "3.12", "3.11")) {
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
        if ($LASTEXITCODE -eq 0 -and $version -in @("3.11", "3.12", "3.13", "3.14")) {
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
    $approval = Read-Host "Python 3.11 through 3.14 is required. Install Python 3.14 for this user with winget now? [y/N]"
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
    & $winget.Source install --id Python.Python.3.14 --exact --scope user
    if ($LASTEXITCODE -ne 0) {
        throw "winget could not install Python 3.14. No further installation steps were run."
    }
    $pythonInstallation = Find-SupportedPython
    if (-not $pythonInstallation) {
        throw "Python 3.14 was installed but is not yet visible to the Python launcher. Close and reopen PowerShell, then run this installer again."
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

if (-not (Get-Command git.exe -ErrorAction SilentlyContinue) -and -not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "Git is required to install directly from the repository. Install Git for Windows, then run this installer again."
}

Invoke-Python -Arguments @("-m", "pip", "install", "--user", "pipx")
Invoke-Python -Arguments @("-m", "pipx", "ensurepath")
Invoke-Python -Arguments @("-m", "pipx", "install", "--force", ("git+" + $RepositoryUrl))

$pipxEnvironmentArguments = @($pythonSelector) + @("-m", "pipx", "environment", "--value", "PIPX_HOME")
$pipxHome = (& $pythonLauncherPath @pipxEnvironmentArguments).Trim()
if ($LASTEXITCODE -ne 0 -or -not $pipxHome) {
    throw "Could not locate the pipx virtual environment after installation."
}
$assistantPython = Join-Path $pipxHome "venvs\anythingllm-pdf-assistant\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $assistantPython -PathType Leaf)) {
    throw "The installed assistant Python executable was not found at $assistantPython."
}

& $assistantPython -m anythingllm_pdf_assistant_cli shortcuts repair
if ($LASTEXITCODE -ne 0) {
    throw "The assistant installed, but its desktop shortcuts could not be created. Run `anythingllm-pdf-assistant shortcuts repair` after resolving the reported error."
}

Write-Host "Installation complete. Start and Stop shortcuts were created on your Desktop."

if (-not (Test-AnythingLLMDesktopInstalled)) {
    Offer-OfficialSetupPage -Name "AnythingLLM Desktop" -Purpose "It is required to upload prepared records and create embeddings; local PDF extraction can still be used without it." -Url "https://anythingllm.com/desktop"
}
if (-not (Test-TesseractInstalled)) {
    Offer-OfficialSetupPage -Name "Tesseract OCR" -Purpose "It is required for scanned or image-only PDFs and the Unstructured hi_res/ocr_only extraction modes." -Url "https://tesseract-ocr.github.io/tessdoc/Installation.html"
}
