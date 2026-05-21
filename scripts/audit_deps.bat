@echo off
rem audit_deps.bat ? supply-chain CVE audit for Windows.
rem Run from any directory. Returns 0 = clean.
setlocal enabledelayedexpansion

for %%I in ("%~dp0..") do set "REPO_ROOT=%%~fI"
cd /d "%REPO_ROOT%"

set "PY="
if exist "%REPO_ROOT%\.venv311\Scripts\python.exe" set "PY=%REPO_ROOT%\.venv311\Scripts\python.exe"
if not defined PY if exist "%REPO_ROOT%\.venv\Scripts\python.exe" set "PY=%REPO_ROOT%\.venv\Scripts\python.exe"
if not defined PY if exist "%REPO_ROOT%\venv\Scripts\python.exe" set "PY=%REPO_ROOT%\venv\Scripts\python.exe"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo FATAL: no Python found.
    exit /b 2
)

echo Using Python:
"%PY%" --version

"%PY%" -m pip_audit --version >nul 2>nul
if %errorlevel% neq 0 (
    echo Installing pip-audit...
    "%PY%" -m pip install --quiet --upgrade pip-audit
)

set /a FAILED=0

call :audit_one "requirements.txt"
call :audit_one "similarity\requirements.txt"
call :audit_one "similarity\requirements-test.txt"

for %%R in (oldcam-v*\requirements.txt) do call :audit_one "%%R"

echo(
if !FAILED! gtr 0 (
    echo === FAILED: !FAILED! requirements file^(s^) have findings ===
    echo See docs\security\HARDENING.md section 8 for remediation.
    exit /b 1
)
echo === All requirements files passed pip-audit. ===
exit /b 0

:audit_one
set "REQ=%~1"
if not exist "%REQ%" goto :eof
echo(
echo === pip-audit: %REQ% ===
"%PY%" -m pip_audit -r "%REQ%" --strict --progress-spinner off
if !errorlevel! neq 0 set /a FAILED+=1
goto :eof