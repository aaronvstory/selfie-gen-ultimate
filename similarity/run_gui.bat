@echo off
setlocal enabledelayedexpansion

cd /d "%~dp0"

set "LOG_FILE=%CD%\launcher_runtime.log"
>> "%LOG_FILE%" echo.
>> "%LOG_FILE%" echo ===============================================================================
>> "%LOG_FILE%" echo [INFO] [%date% %time%] Starting run_gui.bat in %CD%

set "PYTHON_BIN="
if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe -c "import sys, tkinter; raise SystemExit(0 if ((3,9) <= sys.version_info[:2] <= (3,12)) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_BIN=.venv\Scripts\python.exe"
        goto :python_found
    )
)

for %%V in (3.12 3.11 3.10 3.9) do (
    py -%%V -c "import sys, tkinter; raise SystemExit(0 if ((3,9) <= sys.version_info[:2] <= (3,12)) else 1)" >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_BIN=py -%%V"
        goto :python_found
    )
)

python -c "import sys, tkinter; raise SystemExit(0 if ((3,9) <= sys.version_info[:2] <= (3,12)) else 1)" >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_BIN=python"
)

:python_found
if "%PYTHON_BIN%"=="" (
    echo [ERROR] No supported Python found with Tk support (requires 3.9-3.12 + tkinter).
    >> "%LOG_FILE%" echo [ERROR] No supported Python found with Tk support.
    if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" pause
    exit /b 1
)

echo [INFO] Using Python interpreter: %PYTHON_BIN%
>> "%LOG_FILE%" echo [INFO] Using Python interpreter: %PYTHON_BIN%
set TF_USE_LEGACY_KERAS=1
set KERAS_BACKEND=tensorflow

if not exist ".venv\Scripts\activate.bat" (
    echo [INFO] Virtual environment not found. Creating one...
    >> "%LOG_FILE%" echo [INFO] Virtual environment not found. Creating one...
    %PYTHON_BIN% -m venv .venv >> "%LOG_FILE%" 2>&1
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        >> "%LOG_FILE%" echo [ERROR] Failed to create virtual environment.
        if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" pause
        exit /b 1
    )
) else (
    .venv\Scripts\python.exe -c "import sys, tkinter; raise SystemExit(0 if ((3,9) <= sys.version_info[:2] <= (3,12)) else 1)" >nul 2>&1
    if errorlevel 1 (
        echo [INFO] Existing virtual environment uses unsupported Python or lacks Tk support. Recreating...
        >> "%LOG_FILE%" echo [INFO] Existing virtual environment uses unsupported Python or lacks Tk support. Recreating...
        rmdir /s /q .venv
        %PYTHON_BIN% -m venv .venv >> "%LOG_FILE%" 2>&1
        if errorlevel 1 (
            echo [ERROR] Failed to recreate virtual environment.
            >> "%LOG_FILE%" echo [ERROR] Failed to recreate virtual environment.
            if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" pause
            exit /b 1
        )
    ) else (
        echo [INFO] Activating existing virtual environment...
        >> "%LOG_FILE%" echo [INFO] Activating existing virtual environment...
    )
)

echo [INFO] Activating virtual environment...
call .venv\Scripts\activate.bat >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [ERROR] Failed to activate virtual environment.
    >> "%LOG_FILE%" echo [ERROR] Failed to activate virtual environment.
    if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" pause
    exit /b 1
)

echo [INFO] Synchronizing dependencies from requirements.txt...
python -m pip install -r requirements.txt >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [ERROR] Failed to synchronize dependencies from requirements.txt.
    >> "%LOG_FILE%" echo [ERROR] Failed to synchronize dependencies from requirements.txt.
    if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" pause
    exit /b 1
)

echo [INFO] Launching Face Similarity GUI...
>> "%LOG_FILE%" echo [INFO] Launching Face Similarity GUI...
python main.py >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
    echo [ERROR] Application exited with an error (code=%EXIT_CODE%).
    >> "%LOG_FILE%" echo [ERROR] Application exited with an error (code=%EXIT_CODE%).
    if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" pause
)

>> "%LOG_FILE%" echo [INFO] run_gui.bat exiting with code %EXIT_CODE%
endlocal & exit /b %EXIT_CODE%
