$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

function Invoke-ProjectPython {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments)

    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed with exit code $LASTEXITCODE`: $($Arguments -join ' ')"
    }
}

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Run scripts/bootstrap.ps1 first."
}

Push-Location $ProjectRoot
try {
    Invoke-ProjectPython -m ruff check .
    Invoke-ProjectPython -m ruff format --check .
    Invoke-ProjectPython -m pytest
    Invoke-ProjectPython -m tts_cli doctor
} finally {
    Pop-Location
}
