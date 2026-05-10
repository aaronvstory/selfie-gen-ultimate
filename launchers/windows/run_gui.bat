@echo off
setlocal enabledelayedexpansion

for %%I in ("%~dp0..\..") do set "ROOT_DIR=%%~fI"
set "GUI_SCRIPT=%ROOT_DIR%\gui_launcher.py"
set "VENV_DIR=%ROOT_DIR%\venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "REQUIREMENTS=%ROOT_DIR%\requirements.txt"
set "OLDCAM_V7_REQUIREMENTS=%ROOT_DIR%\oldcam-v7\requirements.txt"
set "OLDCAM_V8_REQUIREMENTS=%ROOT_DIR%\oldcam-v8\requirements.txt"
set "DEP_CHECKER=%ROOT_DIR%\dependency_checker.py"
set "DEP_HEALTH_SCRIPT=%ROOT_DIR%\dependency_health_check.py"

if not exist "%VENV_PYTHON%" (
    echo.
    echo  Creating virtual environment...
    echo.
    python -m venv "%VENV_DIR%"
    if !errorlevel! neq 0 (
        echo.
        echo  ERROR: Failed to create venv. Is Python installed and on PATH?
        echo.
        pause
        exit /b 1
    )
    echo  venv created.
    echo.
)

echo.
echo  Syncing dependencies from requirements.txt...
echo.
"%VENV_PYTHON%" -m pip install --upgrade pip >nul 2>&1
"%VENV_PYTHON%" -m pip install --only-binary :all: -r "%REQUIREMENTS%"
if !errorlevel! neq 0 (
    echo.
    echo  Retrying base dependencies without binary constraint...
    "%VENV_PYTHON%" -m pip install -r "%REQUIREMENTS%"
)

for %%R in ("%OLDCAM_V7_REQUIREMENTS%" "%OLDCAM_V8_REQUIREMENTS%") do if exist "%%~R" (
    echo.
    echo  Syncing Oldcam dependencies from %%~nxR...
    "%VENV_PYTHON%" -m pip install --only-binary :all: -r "%%~R"
    if !errorlevel! neq 0 (
        echo  Retrying Oldcam dependencies without binary constraint...
        "%VENV_PYTHON%" -m pip install -r "%%~R"
        if !errorlevel! neq 0 (
            echo.
            echo  WARNING: Oldcam dependencies failed to install after retry.
            echo  WARNING: Oldcam Finish may not work correctly.
            echo.
        ) else (
            echo  Oldcam dependencies installed on retry.
        )
    )
)

echo.
echo  Dependency sync complete.
echo.

if exist "%DEP_HEALTH_SCRIPT%" (
    if exist "%DEP_CHECKER%" (
        echo  Running strict dependency bootstrap...
        "%VENV_PYTHON%" "%DEP_CHECKER%" --auto --enforce-all
        if !errorlevel! neq 0 (
            echo.
            echo  ERROR: Strict dependency bootstrap failed.
            echo.
            pause
            exit /b 1
        )
    )

    echo  Validating runtime dependency health...
    "%VENV_PYTHON%" "%DEP_HEALTH_SCRIPT%" --mode check
    if !errorlevel! neq 0 (
        echo.
        echo  Runtime dependency health check failed. Attempting auto-repair...
        "%VENV_PYTHON%" "%DEP_HEALTH_SCRIPT%" --mode repair
        if !errorlevel! neq 0 (
            echo.
            echo  ERROR: Automatic dependency repair failed.
            echo  ERROR: See messages above for the exact failed import checks.
            echo.
            pause
            exit /b 1
        )
    )
)

:launch
echo Using venv: %VENV_PYTHON%
echo Starting Kling UI GUI...
echo.

set "KLING_GUI_CLI_ERRORS=1"
"%VENV_PYTHON%" -u "%GUI_SCRIPT%"
set "EXIT_CODE=!errorlevel!"

echo.
if !EXIT_CODE! neq 0 (
    echo  CRASH - exit code: !EXIT_CODE!
    echo  Check crash_log.txt for details.
    echo.
)

echo Press any key to close...
pause >nul
endlocal

