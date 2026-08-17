param(
    [switch]$NoInstall,
    [switch]$NoBuild
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $VenvPython)) {
    if ($NoInstall) {
        throw "Virtual environment is missing. Remove -NoInstall or install dependencies first."
    }
    python -m venv (Join-Path $ProjectRoot ".venv")
}

if (-not $NoInstall) {
    & $VenvPython -m pip show piano-midi-score *> $null
    if ($LASTEXITCODE -ne 0) {
        & $VenvPython -m pip install -e "${ProjectRoot}[dev]"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot "frontend\node_modules"))) {
        npm install --prefix (Join-Path $ProjectRoot "frontend")
    }
}

if (-not $NoBuild) {
    npm run build --prefix (Join-Path $ProjectRoot "frontend")
}

Write-Host "Piano MIDI Score: http://127.0.0.1:8000" -ForegroundColor Green
& $VenvPython -m uvicorn app.main:app --app-dir (Join-Path $ProjectRoot "backend") --host 127.0.0.1 --port 8000
