@echo off
setlocal enabledelayedexpansion

for %%I in ("%~dp0..") do set "ROOT_DIR=%%~fI"
set "CLI_SCRIPT=%ROOT_DIR%\kling_automation_ui.py"
set "VENV_DIR=%ROOT_DIR%\venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "REQUIREMENTS=%ROOT_DIR%\requirements.txt"
set "DEP_CHECKER=%ROOT_DIR%\dependency_checker.py"

if not exist "%VENV_PYTHON%" (
    echo.
    echo  Creating virtual environment...
    python -m venv "%VENV_DIR%"
    if !errorlevel! neq 0 (
        echo.
        echo  ERROR: Failed to create venv.
        pause
        exit /b 1
    )
)

echo.
echo  Syncing dependencies...
"%VENV_PYTHON%" -m pip install --upgrade pip >nul 2>&1
"%VENV_PYTHON%" -m pip install -r "%REQUIREMENTS%"
if !errorlevel! neq 0 (
    echo.
    echo  ERROR: dependency install failed.
    pause
    exit /b 1
)

if exist "%DEP_CHECKER%" (
    echo.
    echo  Running dependency check...
    "%VENV_PYTHON%" "%DEP_CHECKER%"
)

echo.
echo  Launching CLI...
"%VENV_PYTHON%" -u "%CLI_SCRIPT%"
set "EXIT_CODE=!errorlevel!"

if !EXIT_CODE! neq 0 (
    echo.
    echo  CLI failed with exit code !EXIT_CODE!.
    pause
)

endlocal & exit /b %EXIT_CODE%
