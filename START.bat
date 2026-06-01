@echo off
setlocal enabledelayedexpansion

rem ============================================================
rem  selfie-gen-ultimate -- one-click Windows launcher (SSD/portable)
rem ============================================================
rem  Cross-platform sibling of START.command (macOS). First run:
rem  seeds the app config from the bundled _user_state snapshot (if
rem  present), then hands off to run_gui.bat which resolves/installs
rem  Python, builds the venv, runs the health check, and launches the
rem  GUI. Subsequent runs detect everything in place and go straight
rem  to the GUI. Safe to double-click from a virgin Windows machine.
rem ============================================================

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "USER_STATE=%SCRIPT_DIR%\_user_state"
set "APP_SUPPORT=%USER_STATE%\app_support"
set "GUI_LAUNCHER=%SCRIPT_DIR%\launchers\windows\run_gui.bat"
set "ROOT_LAUNCHER=%SCRIPT_DIR%\run_gui.bat"

for /f "tokens=1-2 delims==" %%A in ('wmic os get LocalDateTime /value 2^>nul') do if "%%A"=="LocalDateTime" set "WDT=%%B"
set "WDT=%WDT: =%"
set "TS=%WDT:~0,4%-%WDT:~4,2%-%WDT:~6,2% %WDT:~8,2%:%WDT:~10,2%:%WDT:~12,2%"
echo(
echo  ============================================================
echo   selfie-gen-ultimate  --  Windows one-click launcher
echo  ============================================================
echo   [%TS%] Root: %SCRIPT_DIR%
echo(

rem --- Seed config from the bundled snapshot on FIRST run only --------------
rem On Windows the GUI reads kling_config.json from the app dir. If the bundle
rem carried a _user_state\app_support snapshot and no live config exists yet,
rem copy it in. Never overwrite an existing live config.
if not exist "%SCRIPT_DIR%\kling_config.json" (
  if exist "%APP_SUPPORT%\kling_config.json" (
    echo   [%TS%] Seeding config from bundled snapshot...
    copy /Y "%APP_SUPPORT%\kling_config.json" "%SCRIPT_DIR%\kling_config.json" >nul 2>&1
    if exist "%APP_SUPPORT%\ui_config.json" copy /Y "%APP_SUPPORT%\ui_config.json" "%SCRIPT_DIR%\ui_config.json" >nul 2>&1
    if exist "%APP_SUPPORT%\kling_history.json" copy /Y "%APP_SUPPORT%\kling_history.json" "%SCRIPT_DIR%\kling_history.json" >nul 2>&1
    if exist "%APP_SUPPORT%\pricing_cache.json" copy /Y "%APP_SUPPORT%\pricing_cache.json" "%SCRIPT_DIR%\pricing_cache.json" >nul 2>&1
    if exist "%APP_SUPPORT%\model_cache" xcopy /E /I /Y /Q "%APP_SUPPORT%\model_cache" "%SCRIPT_DIR%\model_cache" >nul 2>&1
  ) else (
    echo   [%TS%] No bundled config snapshot -- add API keys in the GUI.
  )
) else (
  echo   [%TS%] Existing config found -- leaving it untouched.
)

rem --- Optional: extract a pre-built venv to skip the slow first install ----
if not exist "%SCRIPT_DIR%\venv\Scripts\python.exe" (
  if exist "%USER_STATE%\venv-windows.tar" (
    echo   [%TS%] Extracting pre-built venv ^(faster than a fresh install^)...
    tar -xf "%USER_STATE%\venv-windows.tar" -C "%SCRIPT_DIR%" >nul 2>&1
    if exist "%SCRIPT_DIR%\venv\Scripts\python.exe" (
      echo   [%TS%] venv extracted.
    ) else (
      echo   [%TS%] venv extract incomplete -- run_gui.bat will build one.
    )
  ) else (
    echo   [%TS%] No pre-built venv -- first launch installs deps ^(5-15 min^).
  )
)

rem --- Hand off to the canonical GUI launcher -------------------------------
echo   [%TS%] Launching GUI...
echo(
if exist "%GUI_LAUNCHER%" (
  call "%GUI_LAUNCHER%" %*
) else if exist "%ROOT_LAUNCHER%" (
  call "%ROOT_LAUNCHER%" %*
) else (
  echo  ERROR: Could not find run_gui.bat ^(launchers\windows\ or root^).
  pause
  endlocal
  exit /b 1
)
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
