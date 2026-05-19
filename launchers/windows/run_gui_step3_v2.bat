@echo off
setlocal EnableExtensions
rem === Step-3 Layout v2 preview launcher (Windows) ===
rem Launches the GUI with the experimental Step-3 layout v2: the orange
rem rPPG frame gets its OWN re-run/file-picker buttons (symmetric with
rem the violet oldcam frame), less vertical cramp. Basic layout stays
rem the default everywhere else. See docs/rppg-wiring.md.
for /f "tokens=1-3" %%a in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd HH:mm:ss"') do set "TS=%%a %%b %%c"
set "ROOT=%~dp0..\.."
pushd "%ROOT%"
echo [%TS%] Step-3 Layout v2 preview  root=%ROOT%
set "SELFIEGEN_STEP3_LAYOUT=v2"
set "PYBIN=%ROOT%\venv\Scripts\python.exe"
if not exist "%PYBIN%" set "PYBIN=%ROOT%\.venv311\Scripts\python.exe"
if not exist "%PYBIN%" set "PYBIN=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PYBIN%" (
  echo [ERROR] no venv python found under %ROOT%
  popd
  exit /b 1
)
echo [%TS%] using python: %PYBIN%  ^(SELFIEGEN_STEP3_LAYOUT=v2^)
"%PYBIN%" -c "from kling_gui import KlingGUIWindow; KlingGUIWindow().run()"
set "RC=%ERRORLEVEL%"
echo [%TS%] GUI exit code: %RC%
popd
exit /b %RC%
