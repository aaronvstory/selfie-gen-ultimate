@echo off
rem sandbox_install.bat - isolated dependency install for security review.
rem Docs: docs\security\HARDENING.md section 5

setlocal enabledelayedexpansion

for %%I in ("%~dp0..") do set "REPO_ROOT=%%~fI"
cd /d "%REPO_ROOT%"

set "SANDBOX_DIR=%REPO_ROOT%\.sandbox-venv"

set "PY="
where python3 >nul 2>nul && set "PY=python3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo FATAL: no Python found in PATH ^(tried python3, python^)
    exit /b 2
)

echo Sandbox install using:
"%PY%" --version
echo(

rem Warn if cloud creds are present.
set "WARNED=0"
for %%V in (AWS_ACCESS_KEY_ID AWS_PROFILE GOOGLE_APPLICATION_CREDENTIALS AZURE_CLIENT_ID GITHUB_TOKEN GH_TOKEN) do (
    if defined %%V (
        if !WARNED!==0 (
            echo WARNING: cloud creds are set in this shell:
            set "WARNED=1"
        )
        echo     %%V
    )
)
if "!WARNED!"=="1" (
    REM Gemini medium on 49702c0 (2026-05-22): %%V is a for-loop
    REM variable and is undefined outside the loop. Print a generic
    REM hint instead of a literal "%V" in the user-visible output.
    echo     Run "set VAR=" for each variable above before continuing, or accept that
    echo     a malicious install will see them.
    echo(
)

if exist "%SANDBOX_DIR%" (
    echo Removing existing sandbox at %SANDBOX_DIR%...
    rmdir /S /Q "%SANDBOX_DIR%"
)
echo Creating fresh sandbox venv at %SANDBOX_DIR%...
"%PY%" -m venv "%SANDBOX_DIR%"
if errorlevel 1 (
    echo FATAL: venv creation failed ^(errorlevel=!errorlevel!^)
    exit /b 4
)

set "SBPY=%SANDBOX_DIR%\Scripts\python.exe"
if not exist "%SBPY%" (
    echo FATAL: sandbox venv creation failed
    exit /b 3
)

rem Codex P1 on 9ffd0d9 (2026-05-22): the prior version ended with
rem unconditional ``exit /b 0`` even if pip install or pip_audit failed.
rem Callers got a false green and continued with a broken / unaudited
rem sandbox. Capture each step's errorlevel and propagate the worst
rem non-zero exit code so CI / wrappers / users get the real status.
set "FINAL_RC=0"

"%SBPY%" -m pip install --quiet --upgrade pip pip-audit
if errorlevel 1 (
    echo ERROR: pip self-upgrade / pip-audit install failed ^(errorlevel=!errorlevel!^)
    rem Propagate the install failure; pip_audit will likely be missing too.
    exit /b 5
)

if exist "requirements-hashed.txt" (
    echo Installing requirements-hashed.txt --require-hashes --only-binary :all: ...
    "%SBPY%" -m pip install --require-hashes --only-binary :all: -r requirements-hashed.txt
    if errorlevel 1 (
        echo ERROR: requirements-hashed.txt install failed ^(errorlevel=!errorlevel!^)
        exit /b 6
    )
) else (
    echo Installing requirements.txt --only-binary :all: ^(no requirements-hashed.txt - tamper detection OFF^)...
    "%SBPY%" -m pip install --only-binary :all: -r requirements.txt
    if errorlevel 1 (
        echo ERROR: requirements.txt install failed ^(errorlevel=!errorlevel!^)
        exit /b 6
    )
)

echo(
echo === Sandbox audit (pip-audit on installed env^) ===
"%SBPY%" -m pip_audit --strict --disable-pip --progress-spinner off
if errorlevel 1 (
    rem pip_audit returns non-zero when vulnerabilities are found.
    rem Surface that as the script's exit code (callers can choose to
    rem treat it as warning vs fail; the script's job is to be honest
    rem about what was found, not to mask it).
    set "FINAL_RC=!errorlevel!"
    echo WARNING: pip_audit reported vulnerabilities ^(exit code !FINAL_RC!^)
)

echo(
echo Sandbox is at: %SANDBOX_DIR%
echo Activate with:  %SANDBOX_DIR%\Scripts\activate.bat
echo Tear down:      rmdir /S /Q "%SANDBOX_DIR%"
exit /b !FINAL_RC!
