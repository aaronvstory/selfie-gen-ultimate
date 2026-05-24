@echo off
setlocal enabledelayedexpansion

for %%I in ("%~dp0..\..") do set "ROOT_DIR=%%~fI"
set "GUI_SCRIPT=%ROOT_DIR%\gui_launcher.py"
set "VENV_DIR=%ROOT_DIR%\venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "REQUIREMENTS=%ROOT_DIR%\requirements.txt"
set "OLDCAM_V7_REQUIREMENTS=%ROOT_DIR%\oldcam-v7\requirements.txt"
set "OLDCAM_V8_REQUIREMENTS=%ROOT_DIR%\oldcam-v8\requirements.txt"
set "OLDCAM_V9_REQUIREMENTS=%ROOT_DIR%\oldcam-v9\requirements.txt"
set "OLDCAM_V10_REQUIREMENTS=%ROOT_DIR%\oldcam-v10\requirements.txt"
set "MEDIAPIPE_SPEC=mediapipe==0.10.35"
set "DEP_CHECKER=%ROOT_DIR%\dependency_checker.py"
set "DEP_HEALTH_SCRIPT=%ROOT_DIR%\dependency_health_check.py"
set "STATE_DIR=%ROOT_DIR%\.launcher_state"
set "LOG_FILE=%STATE_DIR%\launch.log"

if not exist "%STATE_DIR%\" mkdir "%STATE_DIR%"

rem --- Timestamp banner -----------------------------------------------------
for /f "tokens=1-2 delims==" %%A in ('wmic os get LocalDateTime /value 2^>nul') do if "%%A"=="LocalDateTime" set "WMIC_DT=%%B"
set "WMIC_DT=%WMIC_DT: =%"
set "LAUNCH_TS=%WMIC_DT:~0,4%-%WMIC_DT:~4,2%-%WMIC_DT:~6,2% %WMIC_DT:~8,2%:%WMIC_DT:~10,2%:%WMIC_DT:~12,2%"
echo(
echo  ============================================================
echo   Ultimate-Selfie-Gen  --  GUI Launcher
echo  ============================================================
echo   [%LAUNCH_TS%] Launch started
echo   Root: %ROOT_DIR%
echo(
>>"%LOG_FILE%" echo [%LAUNCH_TS%] GUI launch started

rem --- PR #49: bootstrap mutex (concurrent launches must not race pip) -
set "SETUP_LOCK=%STATE_DIR%\setup.lock"
:acquire_setup_lock
md "%SETUP_LOCK%" >nul 2>&1
if !errorlevel! equ 0 goto :setup_lock_acquired
rem Stale-lock check: -d -1 = older than 1 day. Conservative; the .sh
rem path uses a finer 10-min window. Bootstrap should never take >24h.
forfiles /P "%STATE_DIR%" /M setup.lock /D -1 >nul 2>&1
if !errorlevel! equ 0 (
    echo   [setup-lock] removing stale lock
    rd /S /Q "%SETUP_LOCK%" >nul 2>&1
    goto :acquire_setup_lock
)
if not defined SETUP_LOCK_WAIT_LOGGED (
    echo   [setup-lock] another launcher is running dependency setup; waiting...
    set "SETUP_LOCK_WAIT_LOGGED=1"
)
rem Sleep 2s. ping is universally available; timeout needs interactive console.
ping -n 3 127.0.0.1 >nul 2>&1
goto :acquire_setup_lock
:setup_lock_acquired

rem --- Create venv if needed ------------------------------------------------
if not exist "%VENV_PYTHON%" (
    echo   [%LAUNCH_TS%] Creating virtual environment...
    python -m venv "%VENV_DIR%"
    if !errorlevel! neq 0 (
        echo(
        echo  ERROR: Failed to create venv. Is Python installed and on PATH?
        echo(
        pause
        exit /b 1
    )
    echo   Virtual environment created.
    echo(
    del "%STATE_DIR%\deps_*.ok" >nul 2>&1
)

rem --- Build stamp key from req file dates+sizes (no subprocess needed) -----
set "STAMP_KEY="
for %%F in ("%REQUIREMENTS%" "%OLDCAM_V7_REQUIREMENTS%" "%OLDCAM_V8_REQUIREMENTS%" "%OLDCAM_V9_REQUIREMENTS%" "%OLDCAM_V10_REQUIREMENTS%") do (
    if exist "%%~F" set "STAMP_KEY=!STAMP_KEY!%%~tF%%~zF"
)
set "STAMP_KEY=%STAMP_KEY: =_%"
set "STAMP_KEY=%STAMP_KEY:/=-%"
set "STAMP_KEY=%STAMP_KEY::=-%"
set "STAMP=%STATE_DIR%\deps_%STAMP_KEY:~0,60%.ok"

rem --- Skip dep work if stamp is current -----------------------------------
if exist "%STAMP%" (
    echo   [%LAUNCH_TS%] Dependencies up-to-date ^(cached stamp^). Skipping sync.
    echo   Tip: delete .launcher_state\deps_*.ok to force a full re-check.
    echo(
    rem PR #49: release bootstrap mutex before fast-path launch (H1 fix)
    rd /S /Q "%SETUP_LOCK%" >nul 2>&1
    goto :launch
)

rem --- Full dep sync (requirements changed or first run) -------------------
echo   [%LAUNCH_TS%] Requirements changed -- syncing dependencies...
echo(
"%VENV_PYTHON%" -m pip install --upgrade pip >nul 2>&1
call :INSTALL_REQUIREMENTS "%REQUIREMENTS%" "base"
if !errorlevel! neq 0 goto :DEPENDENCY_FAIL

for %%R in ("%OLDCAM_V7_REQUIREMENTS%" "%OLDCAM_V8_REQUIREMENTS%" "%OLDCAM_V9_REQUIREMENTS%" "%OLDCAM_V10_REQUIREMENTS%") do if exist "%%~R" (
    echo(
    call :INSTALL_REQUIREMENTS "%%~R" "oldcam"
    if !errorlevel! neq 0 goto :DEPENDENCY_FAIL
)

echo(
echo   [%LAUNCH_TS%] Dependency sync complete.
echo(

if exist "%DEP_HEALTH_SCRIPT%" (
    if exist "%DEP_CHECKER%" (
        echo   [%LAUNCH_TS%] Running dependency bootstrap...
        "%VENV_PYTHON%" "%DEP_CHECKER%" --auto --enforce-all
        if !errorlevel! neq 0 (
            echo(
            echo  ERROR: Dependency bootstrap failed.
            echo(
            pause
            exit /b 1
        )
    )

    echo   [%LAUNCH_TS%] Validating runtime dependency health...
    "%VENV_PYTHON%" "%DEP_HEALTH_SCRIPT%" --mode check
    if !errorlevel! neq 0 (
        echo(
        echo   [%LAUNCH_TS%] Health check failed. Attempting auto-repair...
        "%VENV_PYTHON%" "%DEP_HEALTH_SCRIPT%" --mode repair
        if !errorlevel! neq 0 (
            echo(
            echo  ERROR: Automatic dependency repair failed.
            echo(
            pause
            exit /b 1
        )
    )
    echo   [%LAUNCH_TS%] Runtime health: OK
)

rem --- Write stamp so next launch skips the above --------------------------
del "%STATE_DIR%\deps_*.ok" >nul 2>&1
>>"%STAMP%" echo %LAUNCH_TS%
echo   [%LAUNCH_TS%] Stamp written. Next launch will skip dep sync.
echo(

rem --- PR #49: release bootstrap mutex BEFORE launching the GUI -------
rd /S /Q "%SETUP_LOCK%" >nul 2>&1

:launch
echo   [%LAUNCH_TS%] Launching GUI...
echo   Venv: %VENV_PYTHON%
echo(

set "KLING_GUI_CLI_ERRORS=1"
"%VENV_PYTHON%" -u "%GUI_SCRIPT%" %*
set "EXIT_CODE=!errorlevel!"

echo(
if !EXIT_CODE! neq 0 (
    echo   [%LAUNCH_TS%] CRASH -- exit code: !EXIT_CODE!
    echo   Check crash_log.txt for details.
    echo(
)

echo  Press any key to close...
pause >nul
endlocal
exit /b %EXIT_CODE%

:DEPENDENCY_FAIL
echo(
echo  ERROR: Dependency bootstrap failed.
echo  MediaPipe is required for Oldcam v9/v10.
echo  Close running Python/GUI processes and retry.
echo  If it still fails, recreate the venv or run dep repair/bootstrap manually.
echo(
pause
endlocal & exit /b 1

:INSTALL_REQUIREMENTS
set "REQ_FILE=%~1"
set "REQ_KIND=%~2"
set "REQ_FILTERED=%TEMP%\selfiegen_req_%RANDOM%_%RANDOM%.txt"
if not exist "%REQ_FILE%" exit /b 0
findstr /V /I /B "mediapipe" "%REQ_FILE%" > "%REQ_FILTERED%"
echo   Syncing %REQ_KIND% deps from %~nx1...
"%VENV_PYTHON%" -m pip install --only-binary :all: -r "%REQ_FILTERED%"
if !errorlevel! neq 0 (
    echo   Retrying without binary constraint...
    "%VENV_PYTHON%" -m pip install -r "%REQ_FILTERED%"
    if !errorlevel! neq 0 (
        del "%REQ_FILTERED%" >nul 2>&1
        exit /b 1
    )
)
findstr /I /R "^[ ]*mediapipe" "%REQ_FILE%" >nul
if !errorlevel! equ 0 (
    echo   Installing MediaPipe separately with --no-deps...
    "%VENV_PYTHON%" -m pip install --no-deps "%MEDIAPIPE_SPEC%"
    if !errorlevel! neq 0 (
        del "%REQ_FILTERED%" >nul 2>&1
        exit /b 1
    )
)
del "%REQ_FILTERED%" >nul 2>&1
exit /b 0