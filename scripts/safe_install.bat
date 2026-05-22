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
REM Codex P1 on 49702c0 (2026-05-22): use call so control returns
REM from npm.cmd / pnpm.cmd / yarn.cmd (Windows batch wrappers). Without
REM call, control transfers to the wrapper and never comes back to
REM this script, so the audit section never executes.
call %*
set install_code=!errorlevel!

if not "!install_code!"=="0" (
    echo [safe-install] install failed ^(exit !install_code!^) - skipping audit 1>&2
    exit /b !install_code!
)

for /f "delims=" %%R in ('git rev-parse --show-toplevel 2^>nul') do set REPO_ROOT=%%R
if "!REPO_ROOT!"=="" set REPO_ROOT=%cd%

set PROJECT_SCRIPT=!REPO_ROOT!\scripts\detect_compromise.py
if not exist "!PROJECT_SCRIPT!" (
    echo [safe-install] no detect_compromise.py - audit skipped 1>&2
    exit /b 0
)

REM Subagent HIGH on b807560 (2026-05-22): the prior code set PY=python
REM unconditionally when python3 was missing, then tried to invoke it.
REM Under the old 3-tier ``GEQ 2 = ALERT`` logic, the resulting exit 127
REM produced a false security alarm. Fix: explicit ``where`` check on
REM the chosen PY + distinct "audit skipped" exit path so a missing
REM python is reported as infra issue, not a security finding.
set "PY="
where python3 >nul 2>nul && set "PY=python3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [safe-install] no python found in PATH - audit skipped 1>&2
    echo [safe-install] install completed but was NOT audited; run scripts\detect_compromise.py manually 1>&2
    exit /b 0
)

echo [safe-install] auditing project after install...
!PY! "!PROJECT_SCRIPT!" --repo-root "!REPO_ROOT!"
set audit_code=!errorlevel!

REM Subagent CRITICAL on b807560 (2026-05-22): detect_compromise.py is
REM a 2-tier producer (0=clean, 1=alerts). The prior caller logic
REM treated audit_code==1 as a warning and exited 0, silently allowing
REM a compromised install to be marked "clean". Fix: any non-zero exit
REM is treated as an alert and propagated.
if not "!audit_code!"=="0" (
    echo [safe-install] AUDIT FOUND ALERTS ^(exit !audit_code!^) - review immediately 1>&2
    echo Recommended next steps: 1>&2
    echo   1. Do NOT run anything in this venv/node_modules 1>&2
    echo   2. Check docs\security\IOC_DETECTION_CHECKLIST.md 1>&2
    echo   3. Run /hulud-kit quick for a full machine scan 1>&2
    exit /b !audit_code!
)
echo [safe-install] install + audit clean
exit /b 0
