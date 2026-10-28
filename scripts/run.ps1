$ErrorActionPreference = 'Stop'
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    if (-not (Test-Path -LiteralPath '.venv/Scripts/python.exe')) {
        throw 'Run scripts/bootstrap.ps1 first.'
    }
    & ./.venv/Scripts/python.exe -m slideguard gui
    if ($LASTEXITCODE -ne 0) { throw 'Application exited with an error.' }
} finally {
    Pop-Location
}
