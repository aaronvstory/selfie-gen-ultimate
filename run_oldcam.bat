@echo off
setlocal enabledelayedexpansion

set "BATCH_DIR=%~dp0"
set "REPO_ROOT=%BATCH_DIR:~0,-1%"
set "STATE_DIR=%REPO_ROOT%\.launcher_state"
if not exist "%STATE_DIR%" mkdir "%STATE_DIR%" >nul 2>&1

set "OLDCAM_LAUNCHER=%BATCH_DIR%oldcam-v9\launcher.py"
set "OLDCAM_REQUIREMENTS=%BATCH_DIR%oldcam-v9\requirements.txt"

if not exist "%OLDCAM_LAUNCHER%" (
    echo ERROR: Missing Oldcam launcher at:
    echo   %OLDCAM_LAUNCHER%
    pause
    exit /b 1
)

set "PYTHON_EXE="
if not "%SELFIEGEN_PYTHON%"=="" (
  "%SELFIEGEN_PYTHON%" -V >nul 2>&1
  if not errorlevel 1 set "PYTHON_EXE=%SELFIEGEN_PYTHON%"
)
if not defined PYTHON_EXE if not "%SELFIEGEN_VENV_DIR%"=="" if exist "%SELFIEGEN_VENV_DIR%\Scripts\python.exe" set "PYTHON_EXE=%SELFIEGEN_VENV_DIR%\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%BATCH_DIR%venv\Scripts\python.exe" set "PYTHON_EXE=%BATCH_DIR%venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist "%BATCH_DIR%.venv\Scripts\python.exe" set "PYTHON_EXE=%BATCH_DIR%.venv\Scripts\python.exe"
if not defined PYTHON_EXE (
  py -3.12 -m venv "%BATCH_DIR%venv" >nul 2>&1 || py -3.11 -m venv "%BATCH_DIR%venv" >nul 2>&1 || python -m venv "%BATCH_DIR%venv" >nul 2>&1
  if exist "%BATCH_DIR%venv\Scripts\python.exe" set "PYTHON_EXE=%BATCH_DIR%venv\Scripts\python.exe"
)
if not defined PYTHON_EXE (
  echo ERROR: Python not found. Install Python or create .\venv first.
  pause
  exit /b 1
)

echo Using Python: %PYTHON_EXE%

set "REQ_HASH=missing"
for /f "tokens=1" %%H in ('certutil -hashfile "%OLDCAM_REQUIREMENTS%" SHA256 ^| findstr /I /R "^[0-9A-F][0-9A-F]"') do set "REQ_HASH=%%H"
set "PY_ID=%PYTHON_EXE::=_%"
set "PY_ID=%PY_ID:\=_%"
set "PY_ID=%PY_ID:/=_%"
set "PY_ID=%PY_ID: =_%"
set "STAMP_FILE=%STATE_DIR%\oldcam_v9_%REQ_HASH%_%PY_ID%.ok"

set "NEED_PIP=1"
if exist "%STAMP_FILE%" (
  "%PYTHON_EXE%" -c "import cv2, numpy, mediapipe" >nul 2>&1
  if not errorlevel 1 set "NEED_PIP=0"
)
if "%NEED_PIP%"=="0" (
    echo Dependencies unchanged. Skipping pip install.
) else if exist "%OLDCAM_REQUIREMENTS%" (
    echo Syncing Oldcam V9 dependencies...
    "%PYTHON_EXE%" -m pip install -r "%OLDCAM_REQUIREMENTS%" >nul 2>&1
    if !errorlevel! neq 0 (
        echo ERROR: Could not auto-install all Oldcam dependencies.
        pause
        exit /b 1
    ) else (
        del /q "%STATE_DIR%\oldcam_v9_*.ok" >nul 2>&1
        > "%STAMP_FILE%" echo ok
    )
)

echo Launching Oldcam V9...
"%PYTHON_EXE%" -u "%OLDCAM_LAUNCHER%" %*
set "EXIT_CODE=!errorlevel!"

echo.
if !EXIT_CODE! neq 0 (
    echo Oldcam exited with code: !EXIT_CODE!
)

if not defined OLDCAM_NO_PAUSE (
    echo Press any key to close...
    pause >nul
)
endlocal
exit /b %EXIT_CODE%
