@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0configure-elevenlabs.ps1" %*
exit /b %ERRORLEVEL%
