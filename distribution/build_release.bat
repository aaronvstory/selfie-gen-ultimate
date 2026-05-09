@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Python is required to build release bundles.
    exit /b 1
)

python build_release.py
if %errorlevel% neq 0 (
    echo Release bundle build failed.
    exit /b 1
)

echo Release bundles created under ..\release\
endlocal
