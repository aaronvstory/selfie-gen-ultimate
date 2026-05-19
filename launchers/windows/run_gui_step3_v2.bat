@echo off
setlocal EnableExtensions EnableDelayedExpansion
rem === Step-3 Layout v2 preview launcher (Windows) ===
rem Launches the GUI with the experimental Step-3 layout v2: the orange
rem rPPG frame gets its OWN re-run/file-picker buttons (symmetric with
rem the violet oldcam frame), less vertical cramp. Basic layout stays
rem the default everywhere else. See docs/rppg-wiring.md.
for /f "tokens=1-2 delims==" %%A in ('wmic os get LocalDateTime /value 2^>nul') do if "%%A"=="LocalDateTime" set "WMIC_DT=%%B"
set "WMIC_DT=%WMIC_DT: =_%"
set "TS=%WMIC_DT:~0,4%-%WMIC_DT:~4,2%-%WMIC_DT:~6,2% %WMIC_DT:~8,2%:%WMIC_DT:~10,2%:%WMIC_DT:~12,2%"
set "ROOT=%~dp0..\.."
pushd "%ROOT%"
echo [%TS%] Step-3 Layout v2 preview  root=%ROOT%
set "SELFIEGEN_STEP3_LAYOUT=v2"
rem Resolve a SUPPORTED Python: version-gate every venv candidate so a
rem stale unsupported venv (3.13/3.14) falls through to .venv311 instead
rem of being selected then failing (CLAUDE.md Hard Rule #9 / PR #39).
set "PYBIN="
if "!PYBIN!"=="" call :check_py "%ROOT%\venv\Scripts\python.exe"
if "!PYBIN!"=="" call :check_py "%ROOT%\.venv311\Scripts\python.exe"
if "!PYBIN!"=="" call :check_py "%ROOT%\.venv\Scripts\python.exe"
if "!PYBIN!"=="" (
  echo [ERROR] no SUPPORTED Python ^(3.9-3.12^) venv found under %ROOT%
  popd
  exit /b 1
)
echo [%TS%] using python: !PYBIN!  ^(SELFIEGEN_STEP3_LAYOUT=v2^)
"!PYBIN!" -c "from kling_gui import KlingGUIWindow; KlingGUIWindow().run()"
set "RC=!ERRORLEVEL!"
echo [%TS%] GUI exit code: !RC!
popd
exit /b !RC!

:check_py
if "%~1"=="" exit /b 1
if not exist "%~1" exit /b 1
"%~1" -c "import sys; raise SystemExit(0 if ((3,9) <= sys.version_info[:2] < (3,13)) else 2)" >nul 2>&1
if errorlevel 1 exit /b 1
set "PYBIN=%~1"
exit /b 0
