@echo off
setlocal enabledelayedexpansion

for %%I in ("%~dp0..\..") do set "ROOT_DIR=%%~fI"
set "CLI_SCRIPT=%ROOT_DIR%\kling_automation_ui.py"
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
set "CONSTRAINTS_FILE=%ROOT_DIR%\constraints.txt"
set "STATE_DIR=%ROOT_DIR%\.launcher_state"
set "LOG_FILE=%STATE_DIR%\launch.log"

if not exist "%STATE_DIR%\" mkdir "%STATE_DIR%"

rem --- Timestamp banner -----------------------------------------------------
set "LAUNCH_TS="
for /f "tokens=1-2 delims==" %%A in ('wmic os get LocalDateTime /value 2^>nul') do if "%%A"=="LocalDateTime" set "WMIC_DT=%%B"
if defined WMIC_DT (
    set "WMIC_DT=!WMIC_DT: =!"
    set "LAUNCH_TS=!WMIC_DT:~0,4!-!WMIC_DT:~4,2!-!WMIC_DT:~6,2! !WMIC_DT:~8,2!:!WMIC_DT:~10,2!:!WMIC_DT:~12,2!"
)
rem wmic is removed on modern Win11 -> PowerShell fallback, then locale
rem date/time, so launch-log timestamps are never blank (gemini MED, PR #66).
if not defined LAUNCH_TS for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'" 2^>nul`) do set "LAUNCH_TS=%%T"
if not defined LAUNCH_TS set "LAUNCH_TS=%DATE% %TIME%"
rem --- Release version (parsed from app_version.py text; no Python needed,
rem --- so it prints even on a truly fresh system before the venv exists).
rem --- Single source of truth: app_version.RELEASE_VERSION -- same constant
rem --- the GUI chip and the release-zip name read, so this auto-tracks builds.
rem --- Parse the line "RELEASE_VERSION = "vX.Y"": take the value after =,
rem --- then strip spaces and quotes. Using delims== (not the fragile
rem --- delims=^" which cmd mis-parses inside a quoted options string).
set "APP_VER="
for /f "tokens=2 delims==" %%V in ('findstr /b /c:^"RELEASE_VERSION^" ^"%ROOT_DIR%\app_version.py^" 2^>nul') do set "APP_VER=%%V"
set "APP_VER=%APP_VER: =%"
set "APP_VER=%APP_VER:"=%"
if not defined APP_VER set "APP_VER=unknown"
echo(
echo  ============================================================
echo   Ultimate-Selfie-Gen  %APP_VER%  --  CLI Launcher
echo  ============================================================
echo   [%LAUNCH_TS%] Launch started  ^(version %APP_VER%^)
echo   Root: %ROOT_DIR%
echo(
>>"%LOG_FILE%" echo [%LAUNCH_TS%] app version %APP_VER%
>>"%LOG_FILE%" echo [%LAUNCH_TS%] CLI launch started

rem --- Resolve a Python interpreter + ensure the venv exists ----------------
rem  Shared resolver: existing venvs, py launcher (py -3.11/-3.12 -- works
rem  WITHOUT 'Add to PATH'), PATH python, common install dirs, then silent
rem  auto-install of Python 3.12 (winget -> python.org). Sets VENV_PYTHON +
rem  RESOLVE_RC in this environment.
if not exist "%VENV_PYTHON%" (
    call "%ROOT_DIR%\scripts\win_resolve_python.bat"
    if not "!RESOLVE_RC!"=="0" (
        echo(
        echo  ERROR: Could not resolve or install a supported Python ^(3.9-3.12^).
        echo  See the messages above and %LOG_FILE% for details.
        echo(
        >>"%LOG_FILE%" echo [%LAUNCH_TS%] ERROR: python resolver failed ^(RESOLVE_RC=!RESOLVE_RC!^)
        pause
        exit /b 1
    )
    rem  Resolver may have adopted/created a different venv than the
    rem  caller's %VENV_PYTHON% guess. Clear the dep stamp INSIDE this
    rem  block so the sync re-runs against the resolved venv -- only when
    rem  the resolver actually ran (venv was missing). Unconditional
    rem  deletion would nuke the stamp every launch + defeat the cache
    rem  (gemini/codex CRITICAL, bot round 2).
    if not "%STATE_DIR%"=="" del "%STATE_DIR%\deps_*.ok" >nul 2>&1
)
rem --- Build stamp key from req file dates+sizes ---------------------------
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
    goto :launch
)

rem --- Full dep sync -------------------------------------------------------
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

if exist "%DEP_CHECKER%" (
    echo(
    echo   [%LAUNCH_TS%] Running dependency bootstrap...
    "%VENV_PYTHON%" "%DEP_CHECKER%" --auto --enforce-all
    if !errorlevel! neq 0 (
        echo(
        echo  ERROR: Dependency bootstrap failed.
        pause
        exit /b 1
    )
)

if exist "%DEP_HEALTH_SCRIPT%" (
    echo(
    echo   [%LAUNCH_TS%] Validating runtime dependency health...
    "%VENV_PYTHON%" "%DEP_HEALTH_SCRIPT%" --mode check
    if !errorlevel! neq 0 (
        echo(
        echo   [%LAUNCH_TS%] Health check failed. Attempting auto-repair...
        "%VENV_PYTHON%" "%DEP_HEALTH_SCRIPT%" --mode repair
        if !errorlevel! neq 0 (
            echo(
            echo  ERROR: Automatic dependency repair failed.
            pause
            exit /b 1
        )
    )
    echo   [%LAUNCH_TS%] Runtime health: OK
)

rem --- Write stamp ---------------------------------------------------------
del "%STATE_DIR%\deps_*.ok" >nul 2>&1
>>"%STAMP%" echo %LAUNCH_TS%
echo   [%LAUNCH_TS%] Stamp written. Next launch will skip dep sync.
echo(

:launch
rem --- Auto-detect NVIDIA + bootstrap CuPy. Runs on BOTH cached + full-sync
rem --- paths (each reaches :launch). Idempotent + cached; never blocks
rem --- launch (script exits 0). Opt-out: set KLING_SKIP_GPU_BOOTSTRAP=1
if exist "%ROOT_DIR%\scripts\gpu_bootstrap.py" (
    "%VENV_PYTHON%" "%ROOT_DIR%\scripts\gpu_bootstrap.py" --quiet-if-cached
)

echo(
echo   [%LAUNCH_TS%] Launching CLI...
echo(
"%VENV_PYTHON%" -u "%CLI_SCRIPT%"
set "EXIT_CODE=!errorlevel!"

if !EXIT_CODE! neq 0 (
    echo(
    echo   [%LAUNCH_TS%] CLI failed with exit code !EXIT_CODE!.
    pause
)

endlocal & exit /b %EXIT_CODE%

:DEPENDENCY_FAIL
echo(
echo  ERROR: Dependency bootstrap failed.
echo  MediaPipe is required for Oldcam v9/v10.
echo  Close running Python/GUI processes and retry.
echo  If it still fails, recreate the venv or run dep repair/bootstrap manually.
pause
endlocal & exit /b 1

:INSTALL_REQUIREMENTS
set "REQ_FILE=%~1"
set "REQ_KIND=%~2"
set "REQ_FILTERED=%TEMP%\selfiegen_req_%RANDOM%_%RANDOM%.txt"
if not exist "%REQ_FILE%" exit /b 0
findstr /V /I /B "mediapipe" "%REQ_FILE%" > "%REQ_FILTERED%"
rem Guard the constraints flag on file existence (GPT review, PR #65):
rem if constraints.txt is somehow absent, degrade to an unconstrained
rem install instead of pip erroring on a missing -c file. Single inner
rem quotes (NOT doubled) so a path with spaces stays one argument.
set "CC="
if exist "%CONSTRAINTS_FILE%" set "CC=-c "%CONSTRAINTS_FILE%""
echo   Syncing %REQ_KIND% deps from %~nx1...
"%VENV_PYTHON%" -m pip install --only-binary :all: !CC! -r "%REQ_FILTERED%"
if !errorlevel! neq 0 (
    echo   Retrying without binary constraint...
    "%VENV_PYTHON%" -m pip install !CC! -r "%REQ_FILTERED%"
    if !errorlevel! neq 0 (
        del "%REQ_FILTERED%" >nul 2>&1
        exit /b 1
    )
)
findstr /I /R "^[ ]*mediapipe" "%REQ_FILE%" >nul
if !errorlevel! equ 0 (
    echo   Installing MediaPipe separately with --no-deps...
    "%VENV_PYTHON%" -m pip install --no-deps !CC! "%MEDIAPIPE_SPEC%"
    if !errorlevel! neq 0 (
        del "%REQ_FILTERED%" >nul 2>&1
        exit /b 1
    )
)
del "%REQ_FILTERED%" >nul 2>&1
exit /b 0
