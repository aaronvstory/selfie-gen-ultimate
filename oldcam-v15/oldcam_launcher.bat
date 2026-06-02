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
echo   Ultimate-Selfie-Gen  --  Oldcam V15 (Temporal Mute)
echo  ============================================================
echo   [%LAUNCH_TS%] Launch started
echo   Script: %SCRIPT_DIR%
echo(

rem --- Locate Python (Rule 9: per-candidate version validation via :check_py)
set "PYTHON_BIN="
set "ENV_KIND="
rem Overrides stay permissive at resolve time; post-resolve gate gives a tailored error if they point at unsupported python.
if not "%SELFIEGEN_PYTHON%"=="" (
  "%SELFIEGEN_PYTHON%" -V >nul 2>&1
  if not errorlevel 1 ( set "PYTHON_BIN=%SELFIEGEN_PYTHON%" & set "ENV_KIND=SELFIEGEN_PYTHON override" )
)
if "!PYTHON_BIN!"=="" if not "%SELFIEGEN_VENV_DIR%"=="" call :check_py "%SELFIEGEN_VENV_DIR%\Scripts\python.exe" "SELFIEGEN_VENV_DIR override" permissive
if "!PYTHON_BIN!"=="" call :check_py "%REPO_ROOT%\venv\Scripts\python.exe" "shared root venv" strict
if "!PYTHON_BIN!"=="" call :check_py "%REPO_ROOT%\.venv311\Scripts\python.exe" "shared root .venv311" strict
if "!PYTHON_BIN!"=="" call :check_py "%REPO_ROOT%\.venv\Scripts\python.exe" "shared root .venv" strict
if "!PYTHON_BIN!"=="" call :check_py ".venv\Scripts\python.exe" "local .venv fallback" strict
if "!PYTHON_BIN!"=="" (
  rem Prefer py launcher 3.11 first per CLAUDE.md Rule 6 spirit.
  py -3.11 -m venv "%REPO_ROOT%\venv" >nul 2>&1 || py -3.12 -m venv "%REPO_ROOT%\venv" >nul 2>&1 || python -m venv "%REPO_ROOT%\venv" >nul 2>&1
  call :check_py "%REPO_ROOT%\venv\Scripts\python.exe" "created shared root venv" strict
)
if "!PYTHON_BIN!"=="" (
  echo   [%LAUNCH_TS%] ERROR: No supported Python (3.9-3.12) found. Install python3.11 (https://www.python.org/downloads/release/python-3119/) and retry.
  set "HAD_ERRORS=1"
  goto DONE
)
set "PYTHON_CMD=!PYTHON_BIN!"
echo   [%LAUNCH_TS%] Python: !PYTHON_CMD! (!ENV_KIND!)

rem --- Defense-in-depth version gate (also catches SELFIEGEN_PYTHON pointing at unsupported python)
"!PYTHON_CMD!" -c "import sys; raise SystemExit(0 if ((3,9) <= sys.version_info[:2] < (3,13)) else 2)" >nul 2>&1
if errorlevel 1 (
  for /f "delims=" %%V in ('"!PYTHON_CMD!" -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2^>nul') do set "PY_ACTUAL=%%V"
  if not "%SELFIEGEN_PYTHON%"=="" (
    echo   [%LAUNCH_TS%] ERROR: SELFIEGEN_PYTHON points at Python !PY_ACTUAL!, but Oldcam v15 requires 3.9-3.12. Unset it or point at python3.11.
  ) else if not "%SELFIEGEN_VENV_DIR%"=="" (
    echo   [%LAUNCH_TS%] ERROR: SELFIEGEN_VENV_DIR points at Python !PY_ACTUAL!, but Oldcam v15 requires 3.9-3.12. Unset it or point at python3.11.
  ) else (
    echo   [%LAUNCH_TS%] ERROR: Resolved Python is !PY_ACTUAL!, outside supported range 3.9-3.12 (resolver bug; please file an issue).
  )
  set "HAD_ERRORS=1"
  goto DONE
)

rem --- V15 needs no MediaPipe / face_landmarker.task: forensic daylight pipeline.

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
set "STAMP=%STATE_DIR%\oldcam_v15_%STAMP_KEY:~0,60%.ok"

set "NEED_PIP=1"
if exist "%STAMP%" (
  "%PYTHON_CMD%" -c "import cv2, numpy" >nul 2>&1
  if not errorlevel 1 set "NEED_PIP=0"
)
if "%NEED_PIP%"=="0" (
  echo   [%LAUNCH_TS%] Dependencies up-to-date ^(cached stamp^). Skipping sync.
  echo(
) else (
  echo   [%LAUNCH_TS%] Syncing Oldcam V15 dependencies...
  "%PYTHON_CMD%" -m pip install !CC! -r "%SCRIPT_DIR%requirements.txt" >nul 2>&1
  if errorlevel 1 (
    echo   [%LAUNCH_TS%] ERROR: Failed to install Oldcam V15 dependencies.
    echo   Close running Python/GUI processes and retry.
    set "HAD_ERRORS=1"
    goto DONE
  )
  for %%F in ("%STATE_DIR%\oldcam_v15_*.ok") do del "%%F" >nul 2>&1
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
"%PYTHON_CMD%" "%SCRIPT_DIR%oldcam.py" "%~1" %EXTRA_ARGS%
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
set "FINAL_EXIT=0"
if defined HAD_ERRORS set "FINAL_EXIT=1"
endlocal & exit /b %FINAL_EXIT%

rem ============================================================
rem :check_py "<path>" "<kind>" [permissive|strict]
rem   - Verifies <path> exists.
rem   - In strict mode (default) also requires Python 3.9-3.12.
rem   - In permissive mode skips the version probe (used for SELFIEGEN_*
rem     overrides so the user gets a clear "your override is wrong" message
rem     vs. a generic resolver bug).
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
