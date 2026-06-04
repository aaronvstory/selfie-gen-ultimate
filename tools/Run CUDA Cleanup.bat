@echo off
REM SelfieGen - one-click CUDA Toolkit PATH cleanup launcher.
REM Double-click this file. It runs the .ps1 next to it, bypassing the
REM PowerShell execution-policy prompt, and the .ps1 self-elevates to Admin.
setlocal
echo Launching CUDA Toolkit cleanup (you will get a Windows admin prompt)...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0cleanup-cuda-toolkit-paths.ps1"
echo.
echo If a User Account Control window appeared, approve it to let the cleanup run.
pause
