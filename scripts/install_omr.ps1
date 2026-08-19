# Installs the Audiveris OMR engine used by the PDF-score upload path.
#
#   .\scripts\install_omr.ps1                # console build via GitHub MSI (recommended for CLI)
#   .\scripts\install_omr.ps1 -Variant winget
#
# Audiveris is a Java application; the MSI bundles its own runtime, so no
# separate JDK is required. Administrative rights may be requested by the
# installer.

[CmdletBinding()]
param(
    [ValidateSet("console", "winget")]
    [string]$Variant = "console"
)

$ErrorActionPreference = "Stop"

function Find-Audiveris {
    $projectRoot = Split-Path -Parent $PSScriptRoot
    $candidates = @(
        (Join-Path $projectRoot "tools\audiveris\Audiveris\Audiveris.exe"),
        "$env:ProgramFiles\Audiveris\Audiveris.exe",
        "${env:ProgramFiles(x86)}\Audiveris\Audiveris.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            return $candidate
        }
    }
    $onPath = Get-Command Audiveris -ErrorAction SilentlyContinue
    if ($onPath) {
        return $onPath.Source
    }
    return $null
}

$existing = Find-Audiveris
if ($existing) {
    Write-Host "Audiveris is already installed: $existing" -ForegroundColor Green
    exit 0
}

if ($Variant -eq "winget") {
    Write-Host "Installing Audiveris via winget ..."
    winget install --id audiveris.org.Audiveris -e --accept-source-agreements --accept-package-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "winget install failed (exit code $LASTEXITCODE)"
    }
}
else {
    Write-Host "Looking up the latest Audiveris release ..."
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/Audiveris/audiveris/releases/latest" -Headers @{ "User-Agent" = "piano-midi-score" }
    $asset = $release.assets | Where-Object { $_.name -like "*windowsConsole*x86_64.msi" } | Select-Object -First 1
    if (-not $asset) {
        $asset = $release.assets | Where-Object { $_.name -like "*windows*x86_64.msi" } | Select-Object -First 1
    }
    if (-not $asset) {
        throw "No Windows MSI asset found in release $($release.tag_name)"
    }
    $msiPath = Join-Path $env:TEMP $asset.name
    Write-Host "Downloading $($asset.name) (about 85 MB) ..."
    Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $msiPath
    # An administrative install only unpacks the MSI payload: it needs no
    # elevation and keeps the whole engine inside the project's tools folder.
    $targetDir = Join-Path (Split-Path -Parent $PSScriptRoot) "tools\audiveris"
    New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
    Write-Host "Extracting Audiveris $($release.tag_name) into $targetDir ..."
    $process = Start-Process msiexec.exe -ArgumentList "/a", "`"$msiPath`"", "/qn", "TARGETDIR=`"$targetDir`"" -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "msiexec failed with exit code $($process.ExitCode); try -Variant winget instead"
    }
    # The payload carries a redundant copy of the MSI itself.
    Get-ChildItem -Path $targetDir -Filter *.msi | Remove-Item -ErrorAction SilentlyContinue
    Remove-Item $msiPath -ErrorAction SilentlyContinue
}

$installed = Find-Audiveris
if (-not $installed) {
    throw "Audiveris was not found after installation; check the installer output"
}
Write-Host "Audiveris installed: $installed" -ForegroundColor Green

# Tesseract language data powers text items (dynamics, tempo words, titles).
$tessdataDir = Join-Path $env:APPDATA "AudiverisLtd\audiveris\config\tessdata"
$engData = Join-Path $tessdataDir "eng.traineddata"
if (-not (Test-Path -LiteralPath $engData)) {
    try {
        New-Item -ItemType Directory -Force -Path $tessdataDir | Out-Null
        Write-Host "Downloading Tesseract language data (eng.traineddata) ..."
        Invoke-WebRequest -Uri "https://raw.githubusercontent.com/tesseract-ocr/tessdata_fast/main/eng.traineddata" -OutFile $engData
    }
    catch {
        Write-Warning "Could not download eng.traineddata; text recognition will be limited. $_"
    }
}
Write-Host "Restart the service, then upload a PDF score in the web UI to recognize it."
