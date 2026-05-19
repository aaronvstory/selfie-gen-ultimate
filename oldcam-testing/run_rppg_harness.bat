@echo off
setlocal EnableExtensions
rem === Permanent rPPG test harness launcher (Windows) ===
rem Runs the real rPPG injector on the permanent Kling fixture and
rem produces an anti-siren REPORT.md. See CLAUDE.md "rPPG Wiring".
for /f "tokens=1-3" %%a in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd HH:mm:ss"') do set "TS=%%a %%b %%c"
set "ROOT=%~dp0.."
pushd "%ROOT%"
>>"%ROOT%\oldcam-testing\rppg_harness_out\harness.log" echo [%TS%] launch %*
echo [%TS%] rPPG harness root=%ROOT%
set "PYBIN=%ROOT%\venv\Scripts\python.exe"
if not exist "%PYBIN%" set "PYBIN=%ROOT%\.venv311\Scripts\python.exe"
if not exist "%PYBIN%" set "PYBIN=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PYBIN%" (
  echo [ERROR] no venv python found under %ROOT%
  popd
  exit /b 1
)
echo [%TS%] using python: %PYBIN%
"%PYBIN%" "%ROOT%\oldcam-testing\rppg_harness.py" %*
set "RC=%ERRORLEVEL%"
echo [%TS%] harness exit code: %RC%
popd
exit /b %RC%
