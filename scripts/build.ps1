$ErrorActionPreference = 'Stop'
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    & ./.venv/Scripts/python.exe -m PyInstaller --onedir --windowed --name SlideGuard --paths src scripts/desktop_entry.py
    if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
    Write-Output 'Build completed. Test dist/SlideGuard on a separate Windows PC before distribution.'
} finally {
    Pop-Location
}
