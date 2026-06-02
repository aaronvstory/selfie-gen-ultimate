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
set "MEDIAPIPE_SPEC=mediapipe==0.10.35"
set "MP_VALIDATE_CMD=import mediapipe; from mediapipe.tasks.python import vision; v=getattr(vision,chr(70)+chr(97)+chr(99)+chr(101)+chr(76)+chr(97)+chr(110)+chr(100)+chr(109)+chr(97)+chr(114)+chr(107)+chr(101)+chr(114),None); exit(0 if v else 1)"

rem --- Timestamp banner
set "WMIC_DT="
for /f "tokens=1-2 delims==" %%A in ('wmic os get LocalDateTime /value 2^>nul') do if "%%A"=="LocalDateTime" set "WMIC_DT=%%B"
set "LAUNCH_TS="
if defined WMIC_DT (
    set "WMIC_DT=!WMIC_DT: =_!"
    set "LAUNCH_TS=!WMIC_DT:~0,4!-!WMIC_DT:~4,2!-!WMIC_DT:~6,2! !WMIC_DT:~8,2!:!WMIC_DT:~10,2!:!WMIC_DT:~12,2!"
)
rem wmic is removed on modern Win11 -> PowerShell fallback, then locale
rem date/time, so log timestamps are never blank (gemini MED, PR #66).
if not defined LAUNCH_TS for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'" 2^>nul`) do set "LAUNCH_TS=%%T"
if not defined LAUNCH_TS set "LAUNCH_TS=%DATE% %TIME%"
echo(
echo  ============================================================
echo   Ultimate-Selfie-Gen  --  Oldcam V9
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

rem --- Locate face_landmarker.task
set "TASK_MODEL_PATH="
if not defined OLDCAM_FACE_LANDMARKER_TASK if exist "%SCRIPT_DIR%face_landmarker.task" set "TASK_MODEL_PATH=%SCRIPT_DIR%face_landmarker.task"
if not defined TASK_MODEL_PATH if exist "%REPO_ROOT%\face_landmarker.task" set "TASK_MODEL_PATH=%REPO_ROOT%\face_landmarker.task"
if not defined TASK_MODEL_PATH if exist "%REPO_ROOT%\..\face_landmarker.task" set "TASK_MODEL_PATH=%REPO_ROOT%\..\face_landmarker.task"
if not defined TASK_MODEL_PATH if exist "%CD%\face_landmarker.task" set "TASK_MODEL_PATH=%CD%\face_landmarker.task"
if defined OLDCAM_FACE_LANDMARKER_TASK set "TASK_MODEL_PATH=%OLDCAM_FACE_LANDMARKER_TASK%"
if not defined TASK_MODEL_PATH (
  echo   [%LAUNCH_TS%] ERROR: face_landmarker.task not found. Oldcam V9 cannot run.
  echo   Searched: %SCRIPT_DIR%face_landmarker.task
  echo           : %REPO_ROOT%\face_landmarker.task
  echo           : %REPO_ROOT%\..\face_landmarker.task
  echo           : %CD%\face_landmarker.task
  set "HAD_ERRORS=1"
  goto DONE
)
set "OLDCAM_FACE_LANDMARKER_TASK=%TASK_MODEL_PATH%"
echo   [%LAUNCH_TS%] Task model: %OLDCAM_FACE_LANDMARKER_TASK%

rem --- Dep stamp: req date+size, no subprocess
set "STAMP_KEY="
for %%F in ("%SCRIPT_DIR%requirements.txt") do set "STAMP_KEY=%%~tF%%~zF"
set "STAMP_KEY=%STAMP_KEY: =_%"
set "STAMP_KEY=%STAMP_KEY:/=-%"
set "STAMP_KEY=%STAMP_KEY::=-%"
set "STAMP=%STATE_DIR%\oldcam_v9_%STAMP_KEY:~0,60%.ok"

set "NEED_PIP=1"
if exist "%STAMP%" (
  "%PYTHON_CMD%" -c "import cv2, numpy, mediapipe" >nul 2>&1
  if not errorlevel 1 set "NEED_PIP=0"
)
if "%NEED_PIP%"=="0" (
  echo   [%LAUNCH_TS%] Dependencies up-to-date ^(cached stamp^). Skipping sync.
  echo(
) else (
  echo   [%LAUNCH_TS%] Syncing Oldcam V9 dependencies...
  set "REQ_FILTERED=%STATE_DIR%\oldcam_v9_req_filtered.txt"
  findstr /V /I /B "mediapipe" "%SCRIPT_DIR%requirements.txt" > "%REQ_FILTERED%"
  "%PYTHON_CMD%" -m pip install !CC! -r "%REQ_FILTERED%" >nul 2>&1
  if errorlevel 1 (
    for %%F in ("%REQ_FILTERED%") do del "%%F" >nul 2>&1
    echo   [%LAUNCH_TS%] ERROR: Failed to install Oldcam V9 dependencies.
    echo   Close running Python/GUI processes and retry.
    set "HAD_ERRORS=1"
    goto DONE
  )
  "%PYTHON_CMD%" -m pip install --force-reinstall --no-deps !CC! "%MEDIAPIPE_SPEC%" >nul 2>&1
  if errorlevel 1 (
    for %%F in ("%REQ_FILTERED%") do del "%%F" >nul 2>&1
    echo   [%LAUNCH_TS%] ERROR: Failed to install MediaPipe for Oldcam V9.
    echo   Close running Python/GUI processes and retry.
    set "HAD_ERRORS=1"
    goto DONE
  )
  for %%F in ("%REQ_FILTERED%") do del "%%F" >nul 2>&1
  "%PYTHON_CMD%" -c "%MP_VALIDATE_CMD%" >nul 2>&1
  if errorlevel 1 (
    echo   [%LAUNCH_TS%] ERROR: MediaPipe Tasks FaceLandmarker API unavailable. Oldcam V9 cannot run.
    echo   Close Python/GUI processes, delete/rebuild venv, and retry.
    set "HAD_ERRORS=1"
    goto DONE
  )
  for %%F in ("%STATE_DIR%\oldcam_v9_*.ok") do del "%%F" >nul 2>&1
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
