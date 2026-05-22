@echo off
REM safe_install.bat - wrap pip install / npm install with post-install audit.
REM
REM Why: postinstall scripts (npm) and setup.py (pip sdist) execute the moment a
REM compromised package lands on disk. Pre-commit hooks don't help here; this
REM wrapper runs the project audit AFTER the install finishes.
REM
REM Usage:
REM   scripts\safe_install.bat pip install <args>
REM   scripts\safe_install.bat npm install <args>
REM
REM Or set a doskey alias:
REM   doskey pipi=scripts\safe_install.bat pip install $*

setlocal enabledelayedexpansion

if "%~1"=="" (
    echo usage: safe_install.bat ^<package-manager^> ^<args...^> 1>&2
    exit /b 2
)

echo [safe-install] running: %*
%*
set install_code=%errorlevel%

if not "%install_code%"=="0" (
    echo [safe-install] install failed ^(exit %install_code%^) - skipping audit 1>&2
    exit /b %install_code%
)

for /f "delims=" %%R in ('git rev-parse --show-toplevel 2^>nul') do set REPO_ROOT=%%R
if "%REPO_ROOT%"=="" set REPO_ROOT=%cd%

set PROJECT_SCRIPT=%REPO_ROOT%\scripts\detect_compromise.py
if not exist "%PROJECT_SCRIPT%" (
    echo [safe-install] no detect_compromise.py - audit skipped 1>&2
    exit /b 0
)

where python3 >nul 2>&1
if errorlevel 1 (set PY=python) else (set PY=python3)

echo [safe-install] auditing project after install...
%PY% "%PROJECT_SCRIPT%" --root "%REPO_ROOT%"
set audit_code=%errorlevel%

if %audit_code% GEQ 2 (
    echo [safe-install] AUDIT FOUND ALERTS - review immediately 1>&2
    echo Recommended next steps: 1>&2
    echo   1. Do NOT run anything in this venv/node_modules 1>&2
    echo   2. Check docs\security\IOC_DETECTION_CHECKLIST.md 1>&2
    echo   3. Run /hulud-kit quick for a full machine scan 1>&2
    exit /b %audit_code%
)
if %audit_code%==1 (
    echo [safe-install] audit has warnings ^(install completed^) 1>&2
    exit /b 0
)
echo [safe-install] install + audit clean
exit /b 0
