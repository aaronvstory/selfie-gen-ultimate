@echo off
setlocal EnableDelayedExpansion

set "ROOT_DIR=%~dp0"
set "CLI_SCRIPT=%ROOT_DIR%kling_automation_ui.py"
set "VENV_PYTHON=%ROOT_DIR%venv\Scripts\python.exe"
set "STATE_DIR=%ROOT_DIR%.launcher_state"
if not exist "%STATE_DIR%" mkdir "%STATE_DIR%" >nul 2>&1

rem --- Timestamp banner
for /f "tokens=1-2 delims==" %%A in ('wmic os get LocalDateTime /value 2^>nul') do if "%%A"=="LocalDateTime" set "WMIC_DT=%%B"
set "WMIC_DT=%WMIC_DT: =_%"
set "LAUNCH_TS=%WMIC_DT:~0,4%-%WMIC_DT:~4,2%-%WMIC_DT:~6,2% %WMIC_DT:~8,2%:%WMIC_DT:~10,2%:%WMIC_DT:~12,2%"
echo(
echo  ============================================================
echo   Ultimate-Selfie-Gen  --  Automation Mode
echo  ============================================================
echo   [%LAUNCH_TS%] Launch started
echo   Root: %ROOT_DIR%
echo(

if not exist "%VENV_PYTHON%" (
  echo   [%LAUNCH_TS%] Creating virtual environment...
  python -m venv "%ROOT_DIR%venv"
  if errorlevel 1 (
    echo   [%LAUNCH_TS%] ERROR: Failed to create venv.
    pause
    exit /b 1
  )
)

rem --- Dep stamp: req date+size
set "STAMP_KEY="
for %%F in ("%ROOT_DIR%requirements.txt") do set "STAMP_KEY=%%~tF%%~zF"
set "STAMP_KEY=%STAMP_KEY: =_%"
set "STAMP_KEY=%STAMP_KEY:/=-%"
set "STAMP_KEY=%STAMP_KEY::=-%"
set "STAMP=%STATE_DIR%\auto_%STAMP_KEY:~0,60%.ok"

set "NEED_PIP=1"
if exist "%STAMP%" set "NEED_PIP=0"
if "%NEED_PIP%"=="0" (
  echo   [%LAUNCH_TS%] Dependencies up-to-date ^(cached stamp^). Skipping sync.
  echo(
) else (
  echo   [%LAUNCH_TS%] Syncing dependencies...
  "%VENV_PYTHON%" -m pip install --upgrade pip >nul 2>&1
  "%VENV_PYTHON%" -m pip install -r "%ROOT_DIR%requirements.txt" >nul 2>&1
  if errorlevel 1 (
    echo   [%LAUNCH_TS%] ERROR: Dependency install failed.
    pause
    exit /b 1
  )
  for %%F in ("%STATE_DIR%\auto_*.ok") do del "%%F" >nul 2>&1
  >>"%STAMP%" echo %LAUNCH_TS%
  echo   [%LAUNCH_TS%] Dependencies installed. Stamp written.
  echo(
)

echo   [%LAUNCH_TS%] Launching automation mode...
"%VENV_PYTHON%" -u "%CLI_SCRIPT%" --auto
set "EXIT_CODE=%ERRORLEVEL%"

echo(
if %EXIT_CODE% neq 0 (
  echo   [%LAUNCH_TS%] Automation CLI finished with code %EXIT_CODE%.
  pause
)

endlocal & exit /b %EXIT_CODE%
