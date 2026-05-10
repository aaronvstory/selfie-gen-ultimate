@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "LOG_FILE=%CD%\launcher_runtime.log"
>> "%LOG_FILE%" echo.
>> "%LOG_FILE%" echo ===============================================================================
>> "%LOG_FILE%" echo [INFO] [%date% %time%] Starting run_gui.bat in %CD%

set "TF_USE_LEGACY_KERAS=1"
set "KERAS_BACKEND=tensorflow"

set "REPO_ROOT="
if exist "..\requirements.txt" if exist "..\kling_automation_ui.py" for %%I in ("..") do set "REPO_ROOT=%%~fI"
if defined REPO_ROOT (
  set "STATE_DIR=%REPO_ROOT%\.launcher_state"
) else (
  set "STATE_DIR=%CD%\.launcher_state"
)
if not exist "%STATE_DIR%" mkdir "%STATE_DIR%" >nul 2>&1

set "STEP=1"
echo ===============================================================================
echo SELFIE GEN ULTIMATE - Similarity GUI
echo ===============================================================================
echo [!STEP!/5] Locating repository root...
if defined REPO_ROOT (
  echo       Found: %REPO_ROOT%
) else (
  echo       Standalone mode: no repo root found.
)

set /a STEP+=1
set "PYTHON_BIN="
set "ENV_KIND="
if not "%SELFIEGEN_PYTHON%"=="" (
  "%SELFIEGEN_PYTHON%" -V >nul 2>&1
  if not errorlevel 1 (
    set "PYTHON_BIN=%SELFIEGEN_PYTHON%"
    set "ENV_KIND=SELFIEGEN_PYTHON override"
  )
)
if "!PYTHON_BIN!"=="" if not "%SELFIEGEN_VENV_DIR%"=="" (
  if exist "%SELFIEGEN_VENV_DIR%\Scripts\python.exe" (
    "%SELFIEGEN_VENV_DIR%\Scripts\python.exe" -V >nul 2>&1
    if not errorlevel 1 (
      set "PYTHON_BIN=%SELFIEGEN_VENV_DIR%\Scripts\python.exe"
      set "ENV_KIND=SELFIEGEN_VENV_DIR override"
    )
  )
)
if "!PYTHON_BIN!"=="" if defined REPO_ROOT if exist "%REPO_ROOT%\venv\Scripts\python.exe" (
  "%REPO_ROOT%\venv\Scripts\python.exe" -V >nul 2>&1
  if not errorlevel 1 (
    set "PYTHON_BIN=%REPO_ROOT%\venv\Scripts\python.exe"
    set "ENV_KIND=shared root venv"
  )
)
if "!PYTHON_BIN!"=="" if defined REPO_ROOT if exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
  "%REPO_ROOT%\.venv\Scripts\python.exe" -V >nul 2>&1
  if not errorlevel 1 (
    set "PYTHON_BIN=%REPO_ROOT%\.venv\Scripts\python.exe"
    set "ENV_KIND=shared root .venv"
  )
)
if "!PYTHON_BIN!"=="" if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -V >nul 2>&1
  if not errorlevel 1 (
    set "PYTHON_BIN=.venv\Scripts\python.exe"
    set "ENV_KIND=local module .venv fallback"
  )
)
if "!PYTHON_BIN!"=="" (
  if defined REPO_ROOT (
    echo [INFO] Creating repo root venv at %REPO_ROOT%\venv
    py -3.12 -m venv "%REPO_ROOT%\venv" >nul 2>&1 || py -3.11 -m venv "%REPO_ROOT%\venv" >nul 2>&1 || python -m venv "%REPO_ROOT%\venv" >nul 2>&1
    if exist "%REPO_ROOT%\venv\Scripts\python.exe" (
      set "PYTHON_BIN=%REPO_ROOT%\venv\Scripts\python.exe"
      set "ENV_KIND=created shared root venv"
    )
  ) else (
    echo [INFO] Creating standalone local .venv
    py -3.12 -m venv .venv >nul 2>&1 || py -3.11 -m venv .venv >nul 2>&1 || python -m venv .venv >nul 2>&1
    if exist ".venv\Scripts\python.exe" (
      set "PYTHON_BIN=.venv\Scripts\python.exe"
      set "ENV_KIND=created local module .venv fallback"
    )
  )
)
if "!PYTHON_BIN!"=="" (
  echo [ERROR] No usable Python environment found.
  >> "%LOG_FILE%" echo [ERROR] No usable Python environment found.
  if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" pause
  exit /b 1
)

echo [!STEP!/5] Selecting Python environment...
echo       Using !ENV_KIND!:
echo       !PYTHON_BIN!
>> "%LOG_FILE%" echo [INFO] Using !ENV_KIND!: !PYTHON_BIN!

set /a STEP+=1
set "REQ_HASH=missing"
for /f "tokens=1" %%H in ('certutil -hashfile "requirements.txt" SHA256 ^| findstr /R "^[0-9A-F][0-9A-F]"') do set "REQ_HASH=%%H"
set "PY_ID=!PYTHON_BIN::=_!"
set "PY_ID=!PY_ID:\=_!"
set "PY_ID=!PY_ID:/=_!"
set "PY_ID=!PY_ID: =_!"
set "STAMP_FILE=%STATE_DIR%\similarity_gui_!REQ_HASH!_!PY_ID!.ok"

echo [!STEP!/5] Checking dependency state...
set "NEED_PIP=1"
if exist "!STAMP_FILE!" (
  "!PYTHON_BIN!" -c "import cv2, numpy; from PIL import Image; import tkinter" >nul 2>&1
  if not errorlevel 1 set "NEED_PIP=0"
)
if "!NEED_PIP!"=="0" (
  echo       Requirements unchanged. Skipping pip install.
  >> "%LOG_FILE%" echo [INFO] Stamp hit. Skipping pip install.
) else (
  echo       Dependencies stale or missing. Installing...
  >> "%LOG_FILE%" echo [INFO] Installing requirements.txt
  "!PYTHON_BIN!" -m pip install -r requirements.txt >> "%LOG_FILE%" 2>&1
  if errorlevel 1 (
    echo [ERROR] Failed to synchronize dependencies from requirements.txt.
    >> "%LOG_FILE%" echo [ERROR] Failed to synchronize dependencies from requirements.txt.
    if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" pause
    exit /b 1
  )
  del /q "%STATE_DIR%\similarity_gui_*.ok" >nul 2>&1
  > "!STAMP_FILE!" echo ok
)

set /a STEP+=1
echo [!STEP!/5] Launching Face Similarity GUI...
echo       Runtime log: %LOG_FILE%
echo       Crash log: %CD%\crash.log
>> "%LOG_FILE%" echo [INFO] Launching Face Similarity GUI...
"!PYTHON_BIN!" main.py >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"

set /a STEP+=1
echo [!STEP!/5] Running...
if not "%EXIT_CODE%"=="0" (
  echo [ERROR] Application exited with an error (code=%EXIT_CODE%).
  >> "%LOG_FILE%" echo [ERROR] Application exited with an error (code=%EXIT_CODE%).
  if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" pause
)
>> "%LOG_FILE%" echo [INFO] run_gui.bat exiting with code %EXIT_CODE%
endlocal & exit /b %EXIT_CODE%
