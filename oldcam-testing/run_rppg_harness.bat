@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem === Permanent rPPG test harness launcher (Windows) ===
rem Runs the real rPPG injector on the permanent Kling fixture and
rem produces an anti-siren REPORT.md. See CLAUDE.md "rPPG Wiring".
set "WMIC_DT="
for /f "tokens=1-2 delims==" %%A in ('wmic os get LocalDateTime /value 2^>nul') do if "%%A"=="LocalDateTime" set "WMIC_DT=%%B"
set "TS="
if defined WMIC_DT (
    set "WMIC_DT=!WMIC_DT: =_!"
    set "TS=!WMIC_DT:~0,4!-!WMIC_DT:~4,2!-!WMIC_DT:~6,2! !WMIC_DT:~8,2!:!WMIC_DT:~10,2!:!WMIC_DT:~12,2!"
)
rem wmic is removed on modern Win11 -> PowerShell fallback, then locale
rem date/time, so log timestamps are never blank (gemini MED, PR #66).
if not defined TS for /f "usebackq delims=" %%T in (`powershell -NoProfile -Command "Get-Date -Format 'yyyy-MM-dd HH:mm:ss'" 2^>nul`) do set "TS=%%T"
if not defined TS set "TS=%DATE% %TIME%"
set "ROOT=%~dp0.."
pushd "%ROOT%"
rem rppg_harness_out/ is gitignored, so on a clean checkout it does not
rem exist yet; create it before the first log append or the >> redirect
rem fails with "The system cannot find the path specified" (CodeRabbit).
if not exist "%ROOT%\oldcam-testing\rppg_harness_out" mkdir "%ROOT%\oldcam-testing\rppg_harness_out"
>>"%ROOT%\oldcam-testing\rppg_harness_out\harness.log" echo [%TS%] launch %*
echo [%TS%] rPPG harness root=%ROOT%
rem Version-gate every venv candidate (CLAUDE.md Hard Rule #9 / PR #39):
rem a stale unsupported venv must fall through to .venv311, not be
rem selected then fail the post-resolve gate.
set "PYBIN="
if "!PYBIN!"=="" call :check_py "%ROOT%\venv\Scripts\python.exe"
if "!PYBIN!"=="" call :check_py "%ROOT%\.venv311\Scripts\python.exe"
if "!PYBIN!"=="" call :check_py "%ROOT%\.venv\Scripts\python.exe"
if "!PYBIN!"=="" (
  echo [ERROR] no SUPPORTED Python ^(3.9-3.12^) venv found under %ROOT%
  popd
  exit /b 1
)
echo [%TS%] using python: !PYBIN!
"!PYBIN!" "%ROOT%\oldcam-testing\rppg_harness.py" %*
set "RC=!ERRORLEVEL!"
echo [%TS%] harness exit code: !RC!
popd
exit /b !RC!

:check_py
if "%~1"=="" exit /b 1
if not exist "%~1" exit /b 1
"%~1" -c "import sys; raise SystemExit(0 if ((3,9) <= sys.version_info[:2] < (3,13)) else 2)" >nul 2>&1
if errorlevel 1 exit /b 1
set "PYBIN=%~1"
exit /b 0
