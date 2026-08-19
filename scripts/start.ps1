param(
    [switch]$NoInstall,
    [switch]$NoBuild,
    # Bind address for the HTTP server.  Use 0.0.0.0 (or -Lan) to share the
    # service with other devices on the local network.
    [string]$Bind = "127.0.0.1",
    [switch]$Lan,
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if ($Lan) {
    $Bind = "0.0.0.0"
}

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

Write-Host "Piano MIDI Score: http://127.0.0.1:$Port" -ForegroundColor Green
if ($Bind -eq "0.0.0.0") {
    $lanIps = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.InterfaceAlias -notlike "*Loopback*" -and $_.IPAddress -notlike "169.254.*" } |
        Select-Object -ExpandProperty IPAddress
    foreach ($ip in $lanIps) {
        Write-Host "LAN: http://${ip}:$Port" -ForegroundColor Green
    }
    Write-Host "LAN access needs a Windows Defender Firewall inbound rule for TCP $Port (admin):"
    Write-Host "  netsh advfirewall firewall add rule name=`"Piano MIDI Score $Port`" dir=in action=allow protocol=TCP localport=$Port"
}
& $VenvPython -m uvicorn app.main:app --app-dir (Join-Path $ProjectRoot "backend") --host $Bind --port $Port
