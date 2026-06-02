@echo off
setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%" >nul

set "REPO_ROOT=%SCRIPT_DIR%.."
for %%I in ("%REPO_ROOT%") do set "REPO_ROOT=%%~fI"
rem Guarded constraints flag (code-review, PR #65): -c only when the file
rem exists, so a missing constraints.txt degrades to an unconstrained
rem install instead of pip erroring. Single inner quotes = space-safe.
set "CC="
if exist "%REPO_ROOT%\constraints.txt" set "CC=-c "%REPO_ROOT%\constraints.txt""
set "STATE_DIR=%REPO_ROOT%\.launcher_state"
if not exist "%STATE_DIR%\" mkdir "%STATE_DIR%"
set "HAD_ERRORS="

rem --- Timestamp banner
for /f "tokens=1-2 delims==" %%A in ('wmic os get LocalDateTime /value 2^>nul') do if "%%A"=="LocalDateTime" set "WMIC_DT=%%B"
set "WMIC_DT=%WMIC_DT: =_%"
set "LAUNCH_TS=%WMIC_DT:~0,4%-%WMIC_DT:~4,2%-%WMIC_DT:~6,2% %WMIC_DT:~8,2%:%WMIC_DT:~10,2%:%WMIC_DT:~12,2%"
echo(
echo  ============================================================
echo   Ultimate-Selfie-Gen  --  Oldcam V7
echo  ============================================================
echo   [%LAUNCH_TS%] Launch started
echo   Script: %SCRIPT_DIR%
echo(

rem --- Locate Python
set "PYTHON_CMD="
if not "%SELFIEGEN_PYTHON%"=="" ("%SELFIEGEN_PYTHON%" -V >nul 2>&1 && set "PYTHON_CMD=%SELFIEGEN_PYTHON%")
if not defined PYTHON_CMD if not "%SELFIEGEN_VENV_DIR%"=="" if exist "%SELFIEGEN_VENV_DIR%\Scripts\python.exe" set "PYTHON_CMD=%SELFIEGEN_VENV_DIR%\Scripts\python.exe"
if not defined PYTHON_CMD if exist "%REPO_ROOT%\venv\Scripts\python.exe" set "PYTHON_CMD=%REPO_ROOT%\venv\Scripts\python.exe"
if not defined PYTHON_CMD if exist "%REPO_ROOT%\.venv\Scripts\python.exe" set "PYTHON_CMD=%REPO_ROOT%\.venv\Scripts\python.exe"
if not defined PYTHON_CMD if exist ".venv\Scripts\python.exe" set "PYTHON_CMD=.venv\Scripts\python.exe"
if not defined PYTHON_CMD (
  py -3.12 -m venv "%REPO_ROOT%\venv" >nul 2>&1 || py -3.11 -m venv "%REPO_ROOT%\venv" >nul 2>&1 || python -m venv "%REPO_ROOT%\venv" >nul 2>&1
  if exist "%REPO_ROOT%\venv\Scripts\python.exe" set "PYTHON_CMD=%REPO_ROOT%\venv\Scripts\python.exe"
)
if not defined PYTHON_CMD (
  echo   [%LAUNCH_TS%] ERROR: Could not find usable Python interpreter.
  set "HAD_ERRORS=1"
  goto DONE
)
echo   [%LAUNCH_TS%] Python: %PYTHON_CMD%

rem --- Python version ID for stamp (certutil-based)
set "PY_ID="
for /f "tokens=2" %%V in ('"%PYTHON_CMD%" -V 2^>^&1') do set "PY_ID=%%V"
if defined PY_ID (
  set "PY_HEX="
  for /f "skip=1 delims=" %%L in ('certutil -hashfile "%PYTHON_CMD%" MD5 2^>nul') do (
    if not defined PY_HEX echo %%L | findstr /I /R "^[0-9A-F][0-9A-F]" >nul 2>&1 && set "PY_HEX=%%L"
  )
  if defined PY_HEX set "PY_ID=%PY_ID%_%PY_HEX%"
)
set "PY_ID=%PY_ID:/=_%"
set "PY_ID=%PY_ID: =_%"

rem --- v2.17: canonical shared-venv preflight (full-set health check +
rem --- repair) BEFORE our own minimal install, so a partial shared venv
rem --- is repaired canonically rather than launching oldcam into a weird
rem --- ImportError. Best-effort (the helper never fails the caller).
if exist "%REPO_ROOT%\scripts\win_preflight_shared_venv.bat" call "%REPO_ROOT%\scripts\win_preflight_shared_venv.bat" "%PYTHON_CMD%" "%REPO_ROOT%"

rem --- Dep stamp: req date+size, no subprocess
set "STAMP_KEY="
for %%F in ("%SCRIPT_DIR%requirements.txt") do set "STAMP_KEY=%%~tF%%~zF"
set "STAMP_KEY=%STAMP_KEY: =_%"
set "STAMP_KEY=%STAMP_KEY:/=-%"
set "STAMP_KEY=%STAMP_KEY::=-%"
set "STAMP=%STATE_DIR%\oldcam_v7_%STAMP_KEY:~0,60%.ok"

set "NEED_PIP=1"
if exist "%STAMP%" (
  "%PYTHON_CMD%" -c "import cv2, numpy" >nul 2>nul
  if not errorlevel 1 set "NEED_PIP=0"
)
if "%NEED_PIP%"=="0" (
  echo   [%LAUNCH_TS%] Dependencies up-to-date ^(cached stamp^). Skipping sync.
  echo(
) else (
  echo   [%LAUNCH_TS%] Syncing Oldcam V7 dependencies...
  "%PYTHON_CMD%" -m pip install !CC! -r "%SCRIPT_DIR%requirements.txt" >nul 2>nul
  if errorlevel 1 (
    echo   [%LAUNCH_TS%] ERROR: Failed to install dependencies.
    set "HAD_ERRORS=1"
    goto DONE
  )
  for %%F in ("%STATE_DIR%\oldcam_v7_*.ok") do del "%%F" >nul 2>&1
  >>"%STAMP%" echo %LAUNCH_TS%
  echo   [%LAUNCH_TS%] Dependencies installed. Stamp written.
  echo(
)

set "EXTRA_ARGS="
if defined OLDCAM_EXTRA_ARGS set "EXTRA_ARGS=%OLDCAM_EXTRA_ARGS%"
if "%~1"=="" goto PICK_FILES
goto PROCESS_ARGS

:PICK_FILES
set "SELECTION_FILE=%TEMP%\oldcam_sel_%RANDOM%%RANDOM%.txt"
powershell -NoProfile -STA -Command "Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.OpenFileDialog; $d.Multiselect=$true; $d.Filter='Media Files|*.mp4;*.mov;*.avi;*.mkv;*.webm;*.m4v;*.jpg;*.jpeg;*.png;*.bmp;*.webp|All Files|*.*'; if ($d.ShowDialog()-eq[System.Windows.Forms.DialogResult]::OK){$d.FileNames|Set-Content -Path '%SELECTION_FILE%'}"
if not exist "%SELECTION_FILE%" goto DONE
for /f "usebackq delims=" %%F in ("%SELECTION_FILE%") do call :PROCESS_ONE "%%F"
for %%F in ("%SELECTION_FILE%") do del "%%F" >nul 2>&1
goto DONE

:PROCESS_ARGS
if "%~1"=="" goto DONE
call :PROCESS_ONE "%~1"
shift
goto PROCESS_ARGS

:PROCESS_ONE
echo   [%LAUNCH_TS%] Processing: %~1
call "%PYTHON_CMD%" "%SCRIPT_DIR%oldcam.py" "%~1" %EXTRA_ARGS%
if not "%ERRORLEVEL%"=="0" set "HAD_ERRORS=1"
exit /b 0

:DONE
echo(
if defined HAD_ERRORS (
  echo   [%LAUNCH_TS%] Finished with errors.
) else (
  echo   [%LAUNCH_TS%] Done.
)
if not defined OLDCAM_NO_PAUSE pause
popd >nul
endlocal
if defined HAD_ERRORS exit /b 1
exit /b 0
