@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "TF_USE_LEGACY_KERAS=1"
set "KERAS_BACKEND=tensorflow"

set "REPO_ROOT="
if exist "..\requirements.txt" if exist "..\kling_automation_ui.py" for %%I in ("..") do set "REPO_ROOT=%%~fI"
rem v2.11 numpy-2 guard: thread the root constraints file into pip so a
rem transitive deepface->numpy resolve cannot upgrade numpy past 1.x.
set "CONSTRAINTS_ARG="
if defined REPO_ROOT if exist "%REPO_ROOT%\constraints.txt" set "CONSTRAINTS_ARG=-c "%REPO_ROOT%\constraints.txt""
if defined REPO_ROOT (
  set "STATE_DIR=%REPO_ROOT%\.launcher_state"
) else (
  set "STATE_DIR=%CD%\.launcher_state"
)
if not exist "%STATE_DIR%" mkdir "%STATE_DIR%" >nul 2>&1
set "LOG_FILE=%STATE_DIR%\similarity_gui.log"

rem --- Timestamp banner
for /f "tokens=1-2 delims==" %%A in ('wmic os get LocalDateTime /value 2^>nul') do if "%%A"=="LocalDateTime" set "WMIC_DT=%%B"
set "WMIC_DT=%WMIC_DT: =_%"
set "LAUNCH_TS=%WMIC_DT:~0,4%-%WMIC_DT:~4,2%-%WMIC_DT:~6,2% %WMIC_DT:~8,2%:%WMIC_DT:~10,2%:%WMIC_DT:~12,2%"
echo(
echo  ============================================================
echo   Ultimate-Selfie-Gen  --  Similarity GUI
echo  ============================================================
echo   [%LAUNCH_TS%] Launch started
echo(
>>"%LOG_FILE%" echo(
>>"%LOG_FILE%" echo ============================================================
>>"%LOG_FILE%" echo [%LAUNCH_TS%] Starting similarity GUI

set "PYTHON_BIN="
set "ENV_KIND="
rem Override stays permissive at resolve-time; gate at line ~95 catches bad versions with a tailored msg.
if not "%SELFIEGEN_PYTHON%"=="" (
  "%SELFIEGEN_PYTHON%" -V >nul 2>&1
  if not errorlevel 1 ( set "PYTHON_BIN=%SELFIEGEN_PYTHON%" & set "ENV_KIND=SELFIEGEN_PYTHON override" )
)
if "!PYTHON_BIN!"=="" if not "%SELFIEGEN_VENV_DIR%"=="" call :check_py "%SELFIEGEN_VENV_DIR%\Scripts\python.exe" "SELFIEGEN_VENV_DIR override" permissive
if "!PYTHON_BIN!"=="" if defined REPO_ROOT call :check_py "%REPO_ROOT%\venv\Scripts\python.exe" "shared root venv" strict
if "!PYTHON_BIN!"=="" if defined REPO_ROOT call :check_py "%REPO_ROOT%\.venv311\Scripts\python.exe" "shared root .venv311" strict
if "!PYTHON_BIN!"=="" if defined REPO_ROOT call :check_py "%REPO_ROOT%\.venv\Scripts\python.exe" "shared root .venv" strict
if "!PYTHON_BIN!"=="" call :check_py ".venv\Scripts\python.exe" "local .venv fallback" strict
if "!PYTHON_BIN!"=="" (
  if defined REPO_ROOT (
    py -3.11 -m venv "%REPO_ROOT%\venv" >nul 2>&1 || py -3.12 -m venv "%REPO_ROOT%\venv" >nul 2>&1 || python -m venv "%REPO_ROOT%\venv" >nul 2>&1
    call :check_py "%REPO_ROOT%\venv\Scripts\python.exe" "created shared root venv" strict
  ) else (
    py -3.11 -m venv .venv >nul 2>&1 || py -3.12 -m venv .venv >nul 2>&1 || python -m venv .venv >nul 2>&1
    call :check_py ".venv\Scripts\python.exe" "created local .venv" strict
  )
)
if "!PYTHON_BIN!"=="" (
  echo   [%LAUNCH_TS%] ERROR: No supported Python (3.9-3.12) found. Install python3.11 (https://www.python.org/downloads/release/python-3119/) and retry.
  >>"%LOG_FILE%" echo [ERROR] No supported Python (3.9-3.12) found.
  if not defined SIMILARITY_LAUNCHED_BY_MAIN pause
  exit /b 1
)
echo   [%LAUNCH_TS%] Python: !ENV_KIND! -- !PYTHON_BIN!
>>"%LOG_FILE%" echo [INFO] Using !ENV_KIND!: !PYTHON_BIN!

rem --- Defense-in-depth version gate (also catches SELFIEGEN_PYTHON pointing at unsupported python)
"!PYTHON_BIN!" -c "import sys; raise SystemExit(0 if ((3,9) <= sys.version_info[:2] < (3,13)) else 2)" >nul 2>&1
if errorlevel 1 (
  for /f "delims=" %%V in ('"!PYTHON_BIN!" -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2^>nul') do set "PY_ACTUAL=%%V"
  if not "%SELFIEGEN_PYTHON%"=="" (
    echo   [%LAUNCH_TS%] ERROR: SELFIEGEN_PYTHON points at Python !PY_ACTUAL!, but Similarity requires 3.9-3.12. Unset it or point at python3.11.
  ) else if not "%SELFIEGEN_VENV_DIR%"=="" (
    echo   [%LAUNCH_TS%] ERROR: SELFIEGEN_VENV_DIR points at Python !PY_ACTUAL!, but Similarity requires 3.9-3.12. Unset it or point at python3.11.
  ) else (
    echo   [%LAUNCH_TS%] ERROR: Resolved Python is !PY_ACTUAL!, outside supported range 3.9-3.12 (resolver bug; please file an issue).
  )
  >>"%LOG_FILE%" echo [ERROR] Unsupported Python version: !PY_ACTUAL!
  if not defined SIMILARITY_LAUNCHED_BY_MAIN pause
  exit /b 1
)

rem --- tkinter check
"!PYTHON_BIN!" -c "import tkinter" >nul 2>&1
if errorlevel 1 (
  echo   [%LAUNCH_TS%] ERROR: tkinter missing. Use a Python build with tkinter for GUI mode.
  >>"%LOG_FILE%" echo [ERROR] tkinter missing.
  if not defined SIMILARITY_LAUNCHED_BY_MAIN pause
  exit /b 1
)

rem --- Dep stamp: req date+size
set "STAMP_KEY="
for %%F in ("requirements.txt") do set "STAMP_KEY=%%~tF%%~zF"
set "STAMP_KEY=%STAMP_KEY: =_%"
set "STAMP_KEY=%STAMP_KEY:/=-%"
set "STAMP_KEY=%STAMP_KEY::=-%"
set "STAMP_FILE=%STATE_DIR%\similarity_gui_%STAMP_KEY:~0,60%.ok"

set "NEED_PIP=1"
if exist "!STAMP_FILE!" (
  "!PYTHON_BIN!" -c "import cv2, numpy; from PIL import Image; import tkinter" >nul 2>&1
  if not errorlevel 1 set "NEED_PIP=0"
)
if "!NEED_PIP!"=="0" (
  echo   [%LAUNCH_TS%] Dependencies up-to-date ^(cached stamp^). Skipping sync.
  >>"%LOG_FILE%" echo [INFO] Stamp hit. Skipping pip install.
  echo(
) else (
  echo   [%LAUNCH_TS%] Synchronizing dependencies from requirements.txt...
  >>"%LOG_FILE%" echo [INFO] Installing requirements.txt
  "!PYTHON_BIN!" -m pip install !CONSTRAINTS_ARG! -r requirements.txt >>"%LOG_FILE%" 2>&1
  if errorlevel 1 (
    echo   [%LAUNCH_TS%] ERROR: Failed to synchronize dependencies.
    >>"%LOG_FILE%" echo [ERROR] Failed to install requirements.txt
    if not defined SIMILARITY_LAUNCHED_BY_MAIN pause
    exit /b 1
  )
  for %%F in ("%STATE_DIR%\similarity_gui_*.ok") do del "%%F" >nul 2>&1
  >>"!STAMP_FILE!" echo %LAUNCH_TS%
  echo   [%LAUNCH_TS%] Dependencies installed. Stamp written.
  echo(
)

echo   [%LAUNCH_TS%] Launching Face Similarity GUI...
>>"%LOG_FILE%" echo [INFO] Launching Face Similarity GUI...
"!PYTHON_BIN!" main.py
set "EXIT_CODE=%ERRORLEVEL%"

echo(
echo   [%LAUNCH_TS%] Application finished with code %EXIT_CODE%.
>>"%LOG_FILE%" echo [INFO] Application finished with code %EXIT_CODE%.
if not defined SIMILARITY_LAUNCHED_BY_MAIN pause

endlocal & exit /b %EXIT_CODE%

rem ============================================================
rem :check_py "<path>" "<kind>" [permissive|strict]
rem   - Checks <path> exists.
rem   - In strict mode (default) also requires Python 3.9-3.12.
rem   - In permissive mode skips the version probe (used for SELFIEGEN_VENV_DIR override
rem     so the post-resolve gate can give a tailored override-specific error).
rem   - On success: sets PYTHON_BIN and ENV_KIND.
rem ============================================================
:check_py
if "%~1"=="" exit /b 1
if not exist "%~1" exit /b 1
if /i "%~3"=="permissive" (
  "%~1" -V >nul 2>&1
  if errorlevel 1 exit /b 1
) else (
  "%~1" -c "import sys; raise SystemExit(0 if ((3,9) <= sys.version_info[:2] < (3,13)) else 2)" >nul 2>&1
  if errorlevel 1 exit /b 1
)
set "PYTHON_BIN=%~1"
set "ENV_KIND=%~2"
exit /b 0
