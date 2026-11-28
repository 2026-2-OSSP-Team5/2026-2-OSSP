$ErrorActionPreference = 'Stop'
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    $branch = git branch --show-current
    if ($LASTEXITCODE -ne 0) { throw 'Could not determine the current branch.' }
    $commit = git rev-parse --short HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Could not determine the current commit.' }
    if (-not $branch) { $branch = 'detached' }
    $branch = $branch -replace '[^a-zA-Z0-9._-]', '-'

    $version = & ./.venv/Scripts/python.exe -c "import tomllib; print(tomllib.load(open('pyproject.toml', 'rb'))['project']['version'])"
    if ($LASTEXITCODE -ne 0) { throw 'Could not read the project version.' }
    if ($version -notmatch '^[a-zA-Z0-9._+-]+$') { throw 'Invalid project version for a release filename.' }

    $changes = git status --porcelain --untracked-files=normal
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect local changes.' }
    $localSuffix = ''
    if ($changes) {
        $localSuffix = '-dirty'
        Write-Warning 'This build includes uncommitted local changes.'
    }
    $buildTime = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
    $releaseName = "SlideGuard-v$version-$branch-$commit$localSuffix-$buildTime"
    $releasePath = "releases/$releaseName"

    & "$PSScriptRoot/build.ps1" -DistPath $releasePath
    $executable = "$releasePath/SlideGuard/SlideGuard.exe"
    if (-not (Test-Path -LiteralPath $executable)) { throw 'The build did not produce SlideGuard.exe.' }

    $zipPath = "$releasePath/$releaseName.zip"
    $sourcePath = (Resolve-Path -LiteralPath "$releasePath/SlideGuard").Path
    $archivePath = [System.IO.Path]::GetFullPath($zipPath)
    $temporaryArchive = "$archivePath.partial"
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    try {
        for ($attempt = 1; $attempt -le 6; $attempt++) {
            try {
                # ZipFile allows concurrent readers, including running applications.
                [System.IO.Compression.ZipFile]::CreateFromDirectory(
                    $sourcePath,
                    $temporaryArchive,
                    [System.IO.Compression.CompressionLevel]::Optimal,
                    $true
                )
                Move-Item -LiteralPath $temporaryArchive -Destination $archivePath
                break
            } catch {
                if (Test-Path -LiteralPath $temporaryArchive) {
                    Remove-Item -LiteralPath $temporaryArchive -Force
                }
                $cause = $_.Exception.GetBaseException()
                $canRetry = $cause -is [System.IO.IOException] -or
                    $cause -is [System.UnauthorizedAccessException]
                if (-not $canRetry -or $attempt -eq 6) { throw }
                Write-Warning "ZIP creation encountered a file access error. Retrying ($attempt/6)..."
                Start-Sleep -Seconds 2
            }
        }
    } finally {
        if (Test-Path -LiteralPath $temporaryArchive) {
            Remove-Item -LiteralPath $temporaryArchive -Force
        }
    }
    Write-Output "EXE: $((Resolve-Path -LiteralPath $executable).Path)"
    Write-Output "ZIP: $((Resolve-Path -LiteralPath $zipPath).Path)"
} finally {
    Pop-Location
}
