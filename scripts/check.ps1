$ErrorActionPreference = 'Stop'
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    & ./.venv/Scripts/python.exe -m ruff check src scripts
    if ($LASTEXITCODE -ne 0) { throw 'Ruff check failed.' }
    & ./.venv/Scripts/python.exe -m ruff format --check src scripts
    if ($LASTEXITCODE -ne 0) { throw 'Ruff formatting check failed.' }
    & ./.venv/Scripts/python.exe -m compileall -q src
    if ($LASTEXITCODE -ne 0) { throw 'Module compilation failed.' }
    & ./.venv/Scripts/python.exe -m slideguard doctor
    if ($LASTEXITCODE -ne 0) { throw 'Environment check failed.' }
} finally {
    Pop-Location
}
