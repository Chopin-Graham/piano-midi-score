param(
    [ValidateSet("transkun", "basic-pitch", "both")]
    [string]$Backend = "transkun",
    [ValidateSet("cu121", "cpu")]
    [string]$TorchBuild = "cu121"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$environmentPath = Join-Path $projectRoot ".venv-audio"
$audioPython = Join-Path $environmentPath "Scripts\python.exe"

if (-not (Test-Path -LiteralPath $audioPython)) {
    py -3.10 -m venv $environmentPath
}

& $audioPython -m pip install --upgrade pip

if ($Backend -in @("transkun", "both")) {
    if ($TorchBuild -eq "cpu") {
        & $audioPython -m pip install "torch==2.5.1" "torchaudio==2.5.1"
    } else {
        & $audioPython -m pip install "torch==2.5.1+cu121" "torchaudio==2.5.1+cu121" --index-url https://download.pytorch.org/whl/cu121
    }
    & $audioPython -m pip install pretty-midi pydub soxr moduleconf scipy librosa==0.11.0 mir-eval
    # Transkun declares ncls for its evaluation utilities, but ncls has no
    # Windows wheel and is not imported by the inference path. Install the
    # official MIT wheel without its training/evaluation-only dependency set.
    & $audioPython -m pip install --no-deps "transkun==2.0.1"
}

if ($Backend -in @("basic-pitch", "both")) {
    & $audioPython -m pip install "basic-pitch==0.4.0"
}

Write-Output "Audio backend ready: $audioPython"
Write-Output "Set PIANO_MIDI_SCORE_AUDIO_PYTHON to this path when using another environment."
