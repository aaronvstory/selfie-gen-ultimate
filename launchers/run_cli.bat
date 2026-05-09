@echo off
setlocal enabledelayedexpansion

for %%I in ("%~dp0..") do set "ROOT_DIR=%%~fI"
set "CLI_SCRIPT=%ROOT_DIR%\kling_automation_ui.py"
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
    python -m venv "%VENV_DIR%"
    if !errorlevel! neq 0 (
        echo.
        echo  ERROR: Failed to create venv.
        pause
        exit /b 1
    )
)

echo.
echo  Syncing dependencies from requirements.txt...
"%VENV_PYTHON%" -m pip install --upgrade pip >nul 2>&1
"%VENV_PYTHON%" -m pip install --only-binary :all: -r "%REQUIREMENTS%"
if !errorlevel! neq 0 (
    echo.
    echo  Retrying base dependencies without binary constraint...
    "%VENV_PYTHON%" -m pip install -r "%REQUIREMENTS%"
    if !errorlevel! neq 0 (
        echo.
        echo  ERROR: Base dependencies failed to install.
        pause
        exit /b 1
    )
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

if exist "%DEP_CHECKER%" (
    echo.
    echo  Running strict dependency bootstrap...
    "%VENV_PYTHON%" "%DEP_CHECKER%" --auto --enforce-all
    if !errorlevel! neq 0 (
        if exist "%DEP_HEALTH_SCRIPT%" (
            echo.
            echo  Strict bootstrap failed. Attempting runtime dependency auto-repair...
            "%VENV_PYTHON%" "%DEP_HEALTH_SCRIPT%" --mode repair
            if !errorlevel! neq 0 (
                echo.
                echo  ERROR: Automatic dependency repair failed.
                pause
                exit /b 1
            )
            echo.
            echo  Re-running strict dependency bootstrap...
            "%VENV_PYTHON%" "%DEP_CHECKER%" --auto --enforce-all
        )
        if !errorlevel! neq 0 (
            echo.
            echo  ERROR: Strict dependency bootstrap failed.
            pause
            exit /b 1
        )
    )
)

if exist "%DEP_HEALTH_SCRIPT%" (
    echo.
    echo  Validating runtime dependency health...
    "%VENV_PYTHON%" "%DEP_HEALTH_SCRIPT%" --mode check
    if !errorlevel! neq 0 (
        echo.
        echo  Runtime dependency health check failed. Attempting auto-repair...
        "%VENV_PYTHON%" "%DEP_HEALTH_SCRIPT%" --mode repair
        if !errorlevel! neq 0 (
            echo.
            echo  ERROR: Automatic dependency repair failed.
            pause
            exit /b 1
        )
    )
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
