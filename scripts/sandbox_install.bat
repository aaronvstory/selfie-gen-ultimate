@echo off
rem sandbox_install.bat ? isolated dependency install for security review.
rem Docs: docs\security\HARDENING.md section 5

setlocal enabledelayedexpansion

for %%I in ("%~dp0..") do set "REPO_ROOT=%%~fI"
cd /d "%REPO_ROOT%"

set "SANDBOX_DIR=%REPO_ROOT%\.sandbox-venv"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
    echo FATAL: no Python found in PATH
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
    echo     Run "set %%V=" for each before continuing, or accept that
    echo     a malicious install will see them.
    echo(
)

if exist "%SANDBOX_DIR%" (
    echo Removing existing sandbox at %SANDBOX_DIR%...
    rmdir /S /Q "%SANDBOX_DIR%"
)
echo Creating fresh sandbox venv at %SANDBOX_DIR%...
"%PY%" -m venv "%SANDBOX_DIR%"

set "SBPY=%SANDBOX_DIR%\Scripts\python.exe"
if not exist "%SBPY%" (
    echo FATAL: sandbox venv creation failed
    exit /b 3
)

"%SBPY%" -m pip install --quiet --upgrade pip pip-audit

if exist "requirements-hashed.txt" (
    echo Installing requirements-hashed.txt --require-hashes --only-binary :all: ...
    "%SBPY%" -m pip install --require-hashes --only-binary :all: -r requirements-hashed.txt
) else (
    echo Installing requirements.txt --only-binary :all: ^(no requirements-hashed.txt - tamper detection OFF^)...
    "%SBPY%" -m pip install --only-binary :all: -r requirements.txt
)

echo(
echo === Sandbox audit (pip-audit on installed env^) ===
"%SBPY%" -m pip_audit --strict --disable-pip --progress-spinner off

echo(
echo Sandbox is at: %SANDBOX_DIR%
echo Activate with:  %SANDBOX_DIR%\Scripts\activate.bat
echo Tear down:      rmdir /S /Q "%SANDBOX_DIR%"
exit /b 0