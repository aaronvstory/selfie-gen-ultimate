@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "LOG_FILE=%CD%\launcher_runtime.log"
>> "%LOG_FILE%" echo.
>> "%LOG_FILE%" echo ===============================================================================
>> "%LOG_FILE%" echo [INFO] [%date% %time%] Starting run_cli.bat in %CD%

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

set "PYTHON_BIN="
set "ENV_KIND="
if not "%SELFIEGEN_PYTHON%"=="" (
  "%SELFIEGEN_PYTHON%" -V >nul 2>&1
  if not errorlevel 1 ( set "PYTHON_BIN=%SELFIEGEN_PYTHON%" & set "ENV_KIND=SELFIEGEN_PYTHON override" )
)
if "!PYTHON_BIN!"=="" if not "%SELFIEGEN_VENV_DIR%"=="" if exist "%SELFIEGEN_VENV_DIR%\Scripts\python.exe" (
  "%SELFIEGEN_VENV_DIR%\Scripts\python.exe" -V >nul 2>&1
  if not errorlevel 1 ( set "PYTHON_BIN=%SELFIEGEN_VENV_DIR%\Scripts\python.exe" & set "ENV_KIND=SELFIEGEN_VENV_DIR override" )
)
if "!PYTHON_BIN!"=="" if defined REPO_ROOT if exist "%REPO_ROOT%\venv\Scripts\python.exe" (
  "%REPO_ROOT%\venv\Scripts\python.exe" -V >nul 2>&1
  if not errorlevel 1 ( set "PYTHON_BIN=%REPO_ROOT%\venv\Scripts\python.exe" & set "ENV_KIND=shared root venv" )
)
if "!PYTHON_BIN!"=="" if defined REPO_ROOT if exist "%REPO_ROOT%\.venv\Scripts\python.exe" (
  "%REPO_ROOT%\.venv\Scripts\python.exe" -V >nul 2>&1
  if not errorlevel 1 ( set "PYTHON_BIN=%REPO_ROOT%\.venv\Scripts\python.exe" & set "ENV_KIND=shared root .venv" )
)
if "!PYTHON_BIN!"=="" if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -V >nul 2>&1
  if not errorlevel 1 ( set "PYTHON_BIN=.venv\Scripts\python.exe" & set "ENV_KIND=local module .venv fallback" )
)
if "!PYTHON_BIN!"=="" (
  if defined REPO_ROOT (
    py -3.12 -m venv "%REPO_ROOT%\venv" >nul 2>&1 || py -3.11 -m venv "%REPO_ROOT%\venv" >nul 2>&1 || python -m venv "%REPO_ROOT%\venv" >nul 2>&1
    if exist "%REPO_ROOT%\venv\Scripts\python.exe" ( set "PYTHON_BIN=%REPO_ROOT%\venv\Scripts\python.exe" & set "ENV_KIND=created shared root venv" )
  ) else (
    py -3.12 -m venv .venv >nul 2>&1 || py -3.11 -m venv .venv >nul 2>&1 || python -m venv .venv >nul 2>&1
    if exist ".venv\Scripts\python.exe" ( set "PYTHON_BIN=.venv\Scripts\python.exe" & set "ENV_KIND=created local module .venv fallback" )
  )
)
if "!PYTHON_BIN!"=="" (
  echo [ERROR] No usable Python environment found.
  >> "%LOG_FILE%" echo [ERROR] No usable Python environment found.
  if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" pause
  exit /b 1
)

echo [INFO] Using !ENV_KIND!: !PYTHON_BIN!
>> "%LOG_FILE%" echo [INFO] Using !ENV_KIND!: !PYTHON_BIN!

set "REQ_HASH=missing"
for /f "tokens=1" %%H in ('certutil -hashfile "requirements.txt" SHA256 ^| findstr /R "^[0-9A-F][0-9A-F]"') do set "REQ_HASH=%%H"
set "PY_ID=!PYTHON_BIN::=_!"
set "PY_ID=!PY_ID:\=_!"
set "PY_ID=!PY_ID:/=_!"
set "PY_ID=!PY_ID: =_!"
set "STAMP_FILE=%STATE_DIR%\similarity_cli_!REQ_HASH!_!PY_ID!.ok"

set "NEED_PIP=1"
if exist "!STAMP_FILE!" (
  "!PYTHON_BIN!" -c "import cv2, numpy; from PIL import Image" >nul 2>&1
  if not errorlevel 1 set "NEED_PIP=0"
)
if "!NEED_PIP!"=="1" (
  echo [INFO] Synchronizing dependencies from requirements.txt...
  >> "%LOG_FILE%" echo [INFO] Synchronizing dependencies from requirements.txt...
  "!PYTHON_BIN!" -m pip install -r requirements.txt >> "%LOG_FILE%" 2>&1
  if errorlevel 1 (
    echo [ERROR] Failed to synchronize dependencies from requirements.txt.
    >> "%LOG_FILE%" echo [ERROR] Failed to synchronize dependencies from requirements.txt.
    if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" pause
    exit /b 1
  )
  del /q "%STATE_DIR%\similarity_cli_*.ok" >nul 2>&1
  > "!STAMP_FILE!" echo ok
) else (
  echo [INFO] Requirements unchanged. Skipping pip install.
  >> "%LOG_FILE%" echo [INFO] Requirements unchanged. Skipping pip install.
)

echo [INFO] Launching Face Similarity CLI...
>> "%LOG_FILE%" echo [INFO] Launching Face Similarity CLI...
if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" (
  "!PYTHON_BIN!" main.py --cli
) else (
  "!PYTHON_BIN!" main.py --cli >> "%LOG_FILE%" 2>&1
)
set "EXIT_CODE=%ERRORLEVEL%"

echo.
echo [INFO] Application finished with code %EXIT_CODE%.
>> "%LOG_FILE%" echo [INFO] Application finished with code %EXIT_CODE%.
if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" pause

endlocal & exit /b %EXIT_CODE%
