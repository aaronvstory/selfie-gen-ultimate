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
rem Strip trailing backslash, but NOT for a drive root (D:\ -> D: would be
rem drive-RELATIVE and break path joins). Guard on not ending in ":\".
if "%SCRIPT_DIR:~-1%"=="\" if not "%SCRIPT_DIR:~-2%"==":\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "USER_STATE=%SCRIPT_DIR%\_user_state"
set "APP_SUPPORT=%USER_STATE%\app_support"
set "GUI_LAUNCHER=%SCRIPT_DIR%\launchers\windows\run_gui.bat"
set "ROOT_LAUNCHER=%SCRIPT_DIR%\run_gui.bat"

rem Timestamp banner ? wmic is removed on modern Win11, so fall back to
rem PowerShell (always present on Win10/11), then to locale date/time
rem (gemini MED, PR #66). Keeps launch logs readable on every Windows.
set "TS="
for /f "tokens=1-2 delims==" %%A in ('wmic os get LocalDateTime /value 2^>nul') do if "%%A"=="LocalDateTime" set "WDT=%%B"
if defined WDT (
  set "WDT=!WDT: =!"
  set "TS=!WDT:~0,4!-!WDT:~4,2!-!WDT:~6,2! !WDT:~8,2!:!WDT:~10,2!:!WDT:~12,2!"
)
if not defined TS for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'" 2^>nul`) do set "TS=%%T"
if not defined TS set "TS=%DATE% %TIME%"
echo(
echo  ============================================================
echo   selfie-gen-ultimate  --  Windows one-click launcher
echo  ============================================================
echo   [%TS%] Root: %SCRIPT_DIR%
echo(

rem --- Seed config from the bundled snapshot, ONCE -------------------------
rem A shipped bundle already carries a sanitized default kling_config.json,
rem so gating on its ABSENCE meant the richer _user_state\app_support
rem snapshot (prompts/UI/keys) was never installed (codex P2). Gate on a
rem one-time marker instead: first run with a snapshot present seeds it
rem (over the pristine default), then writes the marker so later runs never
rem re-clobber the user's own edits.
set "SEED_MARKER=%USER_STATE%\.seeded"
if not exist "%SEED_MARKER%" (
  if exist "%APP_SUPPORT%\kling_config.json" (
    echo   [%TS%] Seeding config from bundled snapshot ^(first run^)...
    copy /Y "%APP_SUPPORT%\kling_config.json" "%SCRIPT_DIR%\kling_config.json" >nul 2>&1
    if exist "%APP_SUPPORT%\ui_config.json" copy /Y "%APP_SUPPORT%\ui_config.json" "%SCRIPT_DIR%\ui_config.json" >nul 2>&1
    if exist "%APP_SUPPORT%\kling_history.json" copy /Y "%APP_SUPPORT%\kling_history.json" "%SCRIPT_DIR%\kling_history.json" >nul 2>&1
    if exist "%APP_SUPPORT%\pricing_cache.json" copy /Y "%APP_SUPPORT%\pricing_cache.json" "%SCRIPT_DIR%\pricing_cache.json" >nul 2>&1
    rem mkdir the dest first + drop /I so xcopy never prompts file-vs-dir on
    rem a single-file model_cache (gemini MED, PR #66) -- keeps it non-interactive.
    if exist "%APP_SUPPORT%\model_cache" (
      if not exist "%SCRIPT_DIR%\model_cache" mkdir "%SCRIPT_DIR%\model_cache" >nul 2>&1
      xcopy /E /Y /Q "%APP_SUPPORT%\model_cache" "%SCRIPT_DIR%\model_cache" >nul 2>&1
    )
    if exist "%USER_STATE%" >"%SEED_MARKER%" echo seeded %TS%
  ) else (
    echo   [%TS%] No bundled config snapshot -- add API keys in the GUI.
  )
) else (
  echo   [%TS%] Config already seeded -- leaving your settings untouched.
)

rem --- venv: extract the bundled tarball if absent, then ALWAYS validate ---
rem If no venv exists and a pre-built tarball is bundled, extract it to skip
rem the slow first install. Then probe WHATEVER venv now exists (freshly
rem extracted OR a stale one left on the SSD by another machine / a prior
rem run): a venv tarball carries the original base-Python path in
rem pyvenv.cfg, so python.exe can exist yet be unusable here. run_gui.bat
rem only rebuilds when python.exe is ABSENT, so we must delete a broken
rem venv ourselves (codex P1) -- otherwise it is handed off and fails.
if not exist "%SCRIPT_DIR%\venv\Scripts\python.exe" (
  if exist "%USER_STATE%\venv-windows.tar" (
    echo   [%TS%] Extracting pre-built venv ^(faster than a fresh install^)...
    tar -xf "%USER_STATE%\venv-windows.tar" -C "%SCRIPT_DIR%" >nul 2>&1
  ) else (
    echo   [%TS%] No pre-built venv -- first launch installs deps ^(5-15 min^).
  )
)
if exist "%SCRIPT_DIR%\venv\Scripts\python.exe" call :probe_venv

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

rem ============================================================
:probe_venv
rem Validate the existing venv interpreter; if it cannot run, delete the
rem venv so run_gui.bat rebuilds it. In a subroutine so `if errorlevel`
rem reads the LIVE errorlevel (a nested if-errorlevel inside parens would
rem capture the pre-block value).
"%SCRIPT_DIR%\venv\Scripts\python.exe" -c "import sys" >nul 2>&1
if errorlevel 1 (
  echo   [%TS%] Bundled/existing venv is not usable here -- removing so it rebuilds.
  rd /S /Q "%SCRIPT_DIR%\venv" >nul 2>&1
) else (
  echo   [%TS%] venv present and verified.
)
goto :eof
