@echo off
setlocal
cd /d "%~dp0\.."

rem Reusable post-processing mode-comparison harness launcher.
rem Runs a source video through oldcam/AA/rPPG/crush modes, scores each, and
rem builds an interactive HTML report. Pass-through args go to mode_compare.py.
rem
rem   run_mode_compare.bat --source "C:\path\clip.mp4"
rem   run_mode_compare.bat --source clip.mp4 --modes oldcam:v13,aa:prime,rppg --open

set "PY=venv\Scripts\python.exe"
if not exist "%PY%" set "PY=.venv311\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" oldcam-testing\mode_compare.py %*
set "EC=%ERRORLEVEL%"
echo.
echo Finished with code %EC%.
pause
exit /b %EC%
