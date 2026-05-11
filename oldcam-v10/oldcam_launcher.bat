@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%" >nul

set "REPO_ROOT=%SCRIPT_DIR%.."
for %%I in ("%REPO_ROOT%") do set "REPO_ROOT=%%~fI"
set "STATE_DIR=%REPO_ROOT%\.launcher_state"
if not exist "%STATE_DIR%" mkdir "%STATE_DIR%" >nul 2>&1
set "HAD_ERRORS="
set "MEDIAPIPE_SPEC=mediapipe>=0.10.14"

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
  echo Could not find usable Python interpreter.
  goto DONE
)

set "REQ_HASH=missing"
for /f "tokens=1" %%H in ('certutil -hashfile "%SCRIPT_DIR%requirements.txt" SHA256 ^| findstr /I /R "^[0-9A-F][0-9A-F]"') do set "REQ_HASH=%%H"
set "PY_ID=%PYTHON_CMD::=_%"
set "PY_ID=%PY_ID:\=_%"
set "PY_ID=%PY_ID:/=_%"
set "PY_ID=%PY_ID: =_%"
set "STAMP_FILE=%STATE_DIR%\oldcam_v10_%REQ_HASH%_%PY_ID%.ok"
set "NEED_PIP=1"
if exist "%STAMP_FILE%" (
  "%PYTHON_CMD%" -c "import cv2, numpy, mediapipe" >nul 2>nul
  if not errorlevel 1 set "NEED_PIP=0"
)
if "%NEED_PIP%"=="1" (
  set "REQ_FILTERED=%TEMP%\selfiegen_oldcam_v10_%RANDOM%_%RANDOM%.txt"
  findstr /V /I /R "^[ ]*mediapipe" "%SCRIPT_DIR%requirements.txt" > "%REQ_FILTERED%"
  "%PYTHON_CMD%" -m pip install -r "%REQ_FILTERED%" >nul 2>nul
  if errorlevel 1 (
    del "%REQ_FILTERED%" >nul 2>&1
    echo Failed to install Oldcam v10 dependencies.
    echo MediaPipe is required for Oldcam v10.
    echo Close running Python/GUI processes and retry.
    echo If it still fails, recreate the venv and rerun.
    set "HAD_ERRORS=1"
    goto DONE
  )
  "%PYTHON_CMD%" -m pip install --no-deps "%MEDIAPIPE_SPEC%" >nul 2>nul
  if errorlevel 1 (
    del "%REQ_FILTERED%" >nul 2>&1
    echo Failed to install MediaPipe required by Oldcam v10.
    echo Close running Python/GUI processes and retry.
    echo If it still fails, recreate the venv and rerun.
    set "HAD_ERRORS=1"
    goto DONE
  )
  del "%REQ_FILTERED%" >nul 2>&1
  del /q "%STATE_DIR%\oldcam_v10_*.ok" >nul 2>&1
  > "%STAMP_FILE%" echo ok
)

set "EXTRA_ARGS="
if defined OLDCAM_EXTRA_ARGS set "EXTRA_ARGS=%OLDCAM_EXTRA_ARGS%"
if "%~1"=="" goto PICK_FILES
goto PROCESS_ARGS

:PICK_FILES
set "SELECTION_FILE=%TEMP%\oldcam_selection_%RANDOM%%RANDOM%.txt"
powershell -NoProfile -STA -Command "Add-Type -AssemblyName System.Windows.Forms; $dialog = New-Object System.Windows.Forms.OpenFileDialog; $dialog.Multiselect = $true; $dialog.Filter = 'Media Files|*.mp4;*.mov;*.avi;*.mkv;*.webm;*.m4v;*.jpg;*.jpeg;*.png;*.bmp;*.webp|All Files|*.*'; if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $dialog.FileNames | Set-Content -Path '%SELECTION_FILE%' }"
if not exist "%SELECTION_FILE%" goto DONE
for /f "usebackq delims=" %%F in ("%SELECTION_FILE%") do call :PROCESS_ONE "%%F"
del "%SELECTION_FILE%" >nul 2>nul
goto DONE

:PROCESS_ARGS
if "%~1"=="" goto DONE
call :PROCESS_ONE "%~1"
shift
goto PROCESS_ARGS

:PROCESS_ONE
call "%PYTHON_CMD%" "%SCRIPT_DIR%oldcam.py" "%~1" %EXTRA_ARGS%
if not "%ERRORLEVEL%"=="0" set "HAD_ERRORS=1"
exit /b 0

:DONE
if not defined OLDCAM_NO_PAUSE pause
popd >nul
endlocal
if defined HAD_ERRORS exit /b 1
exit /b 0
