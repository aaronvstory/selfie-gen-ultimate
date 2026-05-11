@echo off
setlocal enabledelayedexpansion

for %%I in ("%~dp0..\..") do set "ROOT_DIR=%%~fI"
set "CLI_SCRIPT=%ROOT_DIR%\kling_automation_ui.py"
set "VENV_DIR=%ROOT_DIR%\venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "REQUIREMENTS=%ROOT_DIR%\requirements.txt"
set "OLDCAM_V7_REQUIREMENTS=%ROOT_DIR%\oldcam-v7\requirements.txt"
set "OLDCAM_V8_REQUIREMENTS=%ROOT_DIR%\oldcam-v8\requirements.txt"
set "OLDCAM_V9_REQUIREMENTS=%ROOT_DIR%\oldcam-v9\requirements.txt"
set "OLDCAM_V10_REQUIREMENTS=%ROOT_DIR%\oldcam-v10\requirements.txt"
set "MEDIAPIPE_SPEC=mediapipe>=0.10.14"
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
call :INSTALL_REQUIREMENTS "%REQUIREMENTS%" "base"
if !errorlevel! neq 0 goto :DEPENDENCY_FAIL

for %%R in ("%OLDCAM_V7_REQUIREMENTS%" "%OLDCAM_V8_REQUIREMENTS%" "%OLDCAM_V9_REQUIREMENTS%" "%OLDCAM_V10_REQUIREMENTS%") do if exist "%%~R" (
    echo.
    call :INSTALL_REQUIREMENTS "%%~R" "oldcam"
    if !errorlevel! neq 0 goto :DEPENDENCY_FAIL
)

if exist "%DEP_CHECKER%" (
    echo.
    echo  Running strict dependency bootstrap...
    "%VENV_PYTHON%" "%DEP_CHECKER%" --auto --enforce-all
    if !errorlevel! neq 0 (
        echo.
        echo  ERROR: Strict dependency bootstrap failed.
        pause
        exit /b 1
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

:DEPENDENCY_FAIL
echo.
echo  ERROR: Dependency bootstrap failed.
echo  MediaPipe is required for Oldcam v9/v10.
echo  Close running Python/GUI processes and retry.
echo  If it still fails, recreate the venv or run dependency repair/bootstrap manually.
pause
endlocal & exit /b 1

:INSTALL_REQUIREMENTS
set "REQ_FILE=%~1"
set "REQ_KIND=%~2"
set "REQ_FILTERED=%TEMP%\\selfiegen_req_%RANDOM%_%RANDOM%.txt"
if not exist "%REQ_FILE%" (
    exit /b 0
)
findstr /V /I /R "^[ ]*mediapipe" "%REQ_FILE%" > "%REQ_FILTERED%"
echo  Syncing %REQ_KIND% dependencies from %~nx1...
"%VENV_PYTHON%" -m pip install --only-binary :all: -r "%REQ_FILTERED%"
if !errorlevel! neq 0 (
    echo  Retrying %REQ_KIND% dependencies without binary constraint...
    "%VENV_PYTHON%" -m pip install -r "%REQ_FILTERED%"
    if !errorlevel! neq 0 (
        del "%REQ_FILTERED%" >nul 2>&1
        exit /b 1
    )
)
findstr /I /R "^[ ]*mediapipe" "%REQ_FILE%" >nul
if !errorlevel! equ 0 (
    echo  Installing MediaPipe separately with --no-deps...
    "%VENV_PYTHON%" -m pip install --no-deps "%MEDIAPIPE_SPEC%"
    if !errorlevel! neq 0 (
        del "%REQ_FILTERED%" >nul 2>&1
        exit /b 1
    )
)
del "%REQ_FILTERED%" >nul 2>&1
exit /b 0

