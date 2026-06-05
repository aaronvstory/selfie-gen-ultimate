@echo off
REM ============================================================
REM  SelfieGen - CUDA cleanup (fixes rPPG running on slow CPU)
REM  Just double-click this file. Approve the admin popup.
REM ============================================================
setlocal
title SelfieGen CUDA Cleanup
echo(
echo  ============================================================
echo   SelfieGen - CUDA cleanup
echo  ============================================================
echo(
echo  This fixes rPPG when it keeps running on the CPU (slow).
echo  It removes an OLD CUDA Toolkit from your PATH so the app
echo  can use its own bundled CUDA instead.
echo(
echo  SAFE: it backs everything up to your Desktop first, asks
echo  before changing anything, and does NOT remove your NVIDIA
echo  graphics driver or touch your SelfieGen install.
echo(
set /p GO=Type Y then Enter to continue (anything else cancels): 
if /I not "%GO%"=="Y" goto cancel
echo(
echo  Starting... approve the Windows Administrator popup if it appears.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0cuda-cleanup.ps1"
goto done
:cancel
echo(
echo  Cancelled. Nothing was changed.
:done
echo(
pause
