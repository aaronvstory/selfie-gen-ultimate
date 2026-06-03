@echo off
setlocal
rem ============================================================
rem  Ultimate-Selfie-Gen  --  Automation BATCH launcher (headless)
rem ------------------------------------------------------------
rem  Thin wrapper: delegates to the canonical CLI launcher chain
rem  (launchers\windows\run_cli.bat) so the FULL v2.17 dependency
rem  bootstrap runs -- GPU/OS-aware torch, CuPy, mediapipe --no-deps
rem  + matplotlib runtime deps, health-gated stamp, dependency_checker
rem  preflight -- then launches the automation pipeline NON-INTERACTIVELY
rem  via "kling_automation_ui.py --batch".
rem
rem  Usage:
rem    run_auto.bat "C:\path\to\cases_root" [--limit N] [--reprocess MODE]
rem
rem  Exit code is the pipeline status (0=success, non-zero=failure) so
rem  Windows Task Scheduler / CI can detect failed jobs.
rem ============================================================
set "ROOT_DIR=%~dp0"
set "TARGET=%ROOT_DIR%launchers\run_cli.bat"

if not exist "%TARGET%" (
    echo.
    echo ERROR: Missing launcher: %TARGET%
    echo.
    rem No pause: this is the headless batch entry; pausing would hang an
    rem unattended cron / Task Scheduler job forever (code-review Gemini HIGH).
    exit /b 1
)

rem Headless: tell the canonical launcher chain to NOT pause on failure
rem (cron / Task Scheduler would hang on a pause). The chain guards every
rem pause behind: if not defined KLING_NONINTERACTIVE pause.
set "KLING_NONINTERACTIVE=1"
rem Inject --batch and forward every user arg (root folder, --limit, etc).
call "%TARGET%" --batch %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
