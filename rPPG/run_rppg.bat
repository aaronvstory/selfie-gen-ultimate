@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem rPPG injector launcher -- runs off the repo main venv.
rem This dir is gitignored (sensitive friend tool); never committed.
rem All deps already live in the shared repo venv -- no pip step.

rem Codex P1 / P2 (2026-05-21): when invoked from a Python subprocess
rem (queue_manager._rppg_video, automation/rppg.py:run_rppg) the four
rem error-path pause + end-of-file pause statements blocked indefinitely
rem waiting for keypress on a hidden stdin. Now suppressed via the
rem KLING_NO_PAUSE env var which the Python callers always set; manual
rem double-click users still get the pauses (var unset). The PAUSE alias
rem call expands to "pause" or nothing per the gate.
set "PAUSE=pause"
if defined KLING_NO_PAUSE set "PAUSE=rem skip_pause"

set "REPO_ROOT="
if exist "..\requirements.txt" if exist "..\kling_automation_ui.py" for %%I in ("..") do set "REPO_ROOT=%%~fI"
if not defined REPO_ROOT (
  echo  [ERROR] Could not locate repo root from %CD%.
  %PAUSE%
  exit /b 1
)
set "STATE_DIR=%REPO_ROOT%\.launcher_state"
if not exist "%STATE_DIR%" mkdir "%STATE_DIR%" >nul 2>&1
set "LOG_FILE=%STATE_DIR%\rppg.log"

set "PYTHON_BIN="
set "ENV_KIND="
if not "%SELFIEGEN_PYTHON%"=="" (
  "%SELFIEGEN_PYTHON%" -V >nul 2>&1
  if not errorlevel 1 ( set "PYTHON_BIN=%SELFIEGEN_PYTHON%" & set "ENV_KIND=SELFIEGEN_PYTHON override" )
)
if "!PYTHON_BIN!"=="" call :check_py "%REPO_ROOT%\venv\Scripts\python.exe" "shared root venv"
if "!PYTHON_BIN!"=="" call :check_py "%REPO_ROOT%\.venv311\Scripts\python.exe" "shared root .venv311"
if "!PYTHON_BIN!"=="" call :check_py "%REPO_ROOT%\.venv\Scripts\python.exe" "shared root .venv"
if "!PYTHON_BIN!"=="" (
  echo   ERROR: No supported Python ^(3.9-3.12^) found in the repo venv.
  echo   Create it: py -3.11 -m venv "%REPO_ROOT%\venv" then pip install -r requirements.txt
  >>"%LOG_FILE%" echo [ERROR] No supported Python found.
  %PAUSE%
  exit /b 1
)
echo   Python: !ENV_KIND! -- !PYTHON_BIN!
>>"%LOG_FILE%" echo [INFO] Using !ENV_KIND!: !PYTHON_BIN!

"!PYTHON_BIN!" -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] < (3,13) else 2)" >nul 2>&1
if errorlevel 1 (
  echo   ERROR: Resolved Python outside supported range 3.9-3.12.
  >>"%LOG_FILE%" echo [ERROR] Unsupported Python after resolve.
  %PAUSE%
  exit /b 1
)
"!PYTHON_BIN!" -c "import cv2, numpy, mediapipe, scipy" >nul 2>&1
if errorlevel 1 (
  echo   ERROR: repo venv missing cv2/numpy/mediapipe/scipy.
  echo   Sync: "%REPO_ROOT%\venv\Scripts\pip" install -r "%REPO_ROOT%\requirements.txt"
  >>"%LOG_FILE%" echo [ERROR] Core imports missing in repo venv.
  %PAUSE%
  exit /b 1
)
if exist "%REPO_ROOT%\face_landmarker.task" set "MEDIAPIPE_FACE_LANDMARKER_MODEL=%REPO_ROOT%\face_landmarker.task"
rem rppg_injector visualize_analysis() calls plt.show() which BLOCKS on a
rem GUI window; force headless Agg so it never waits for a window close.
set "MPLBACKEND=Agg"

echo   Launching rppg_injector.py %*
>>"%LOG_FILE%" echo [INFO] Launching rppg_injector.py %*
"!PYTHON_BIN!" rppg_injector.py %*
set "EXIT_CODE=%ERRORLEVEL%"
echo   Finished with code %EXIT_CODE%.
>>"%LOG_FILE%" echo [INFO] Finished with code %EXIT_CODE%.
%PAUSE%
exit /b %EXIT_CODE%

:check_py
if "%~1"=="" exit /b 1
if not exist "%~1" exit /b 1
"%~1" -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] < (3,13) else 2)" >nul 2>&1
if errorlevel 1 exit /b 1
set "PYTHON_BIN=%~1"
set "ENV_KIND=%~2"
exit /b 0