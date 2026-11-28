@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build-release.ps1"
set "buildExitCode=%ERRORLEVEL%"
pause
exit /b %buildExitCode%
