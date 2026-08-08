$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Run scripts/bootstrap.ps1 first."
}

Push-Location $ProjectRoot
try {
    & $Python -m ruff check .
    & $Python -m ruff format --check .
    & $Python -m pytest
    & $Python -m tts_cli doctor
} finally {
    Pop-Location
}
