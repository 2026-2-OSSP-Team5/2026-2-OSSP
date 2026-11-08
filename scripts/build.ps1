param(
    [string]$DistPath = 'dist'
)

$ErrorActionPreference = 'Stop'
$originalPath = $env:PATH
$originalPyInstallerConfig = $env:PYINSTALLER_CONFIG_DIR
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    $venvScripts = (Resolve-Path -LiteralPath '.venv/Scripts').Path

    # Native dependency discovery must not pick up DLLs from other tools on PATH.
    # Qt uses the ICU libraries supplied by Windows.
    $env:PATH = "$venvScripts;$env:SystemRoot\System32;$env:SystemRoot"
    $env:PYINSTALLER_CONFIG_DIR = (New-Item -ItemType Directory -Force -Path 'build/pyinstaller-cache').FullName
    & "$venvScripts/python.exe" -m PyInstaller --clean --onedir --windowed --name SlideGuard `
        --distpath $DistPath --paths src scripts/desktop_entry.py
    if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
    Write-Output "Build completed in $DistPath/SlideGuard."
} finally {
    $env:PATH = $originalPath
    $env:PYINSTALLER_CONFIG_DIR = $originalPyInstallerConfig
    Pop-Location
}
