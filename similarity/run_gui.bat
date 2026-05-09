@echo off
setlocal enabledelayedexpansion

:: Change to the directory of this script
cd /d "%~dp0"

:: Select a supported Python interpreter with Tk support for GUI mode.
:: Probe order: existing venv -> py launcher versions -> python fallback.
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
    echo Install a Tk-enabled Python 3.12 and retry.
    pause
    exit /b 1
)

echo [INFO] Using Python interpreter: %PYTHON_BIN%
set TF_USE_LEGACY_KERAS=1
set KERAS_BACKEND=tensorflow

:: Check if .venv exists
if not exist ".venv\Scripts\activate.bat" (
    echo [INFO] Virtual environment not found. Creating one...
    %PYTHON_BIN% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )
) else (
    .venv\Scripts\python.exe -c "import sys, tkinter; raise SystemExit(0 if ((3,9) <= sys.version_info[:2] <= (3,12)) else 1)" >nul 2>&1
    if errorlevel 1 (
        echo [INFO] Existing virtual environment uses unsupported Python or lacks Tk support. Recreating...
        rmdir /s /q .venv
        %PYTHON_BIN% -m venv .venv
        if errorlevel 1 (
            echo [ERROR] Failed to recreate virtual environment.
            pause
            exit /b 1
        )
    ) else (
        echo [INFO] Activating existing virtual environment...
    )
)

echo [INFO] Activating virtual environment...
call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Failed to activate virtual environment.
    pause
    exit /b 1
)

echo [INFO] Synchronizing dependencies from requirements.txt...
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to synchronize dependencies from requirements.txt.
    pause
    exit /b 1
)

:: Run the application
echo [INFO] Launching Face Similarity GUI...
python main.py
if errorlevel 1 (
    echo [ERROR] Application exited with an error.
    pause
)

endlocal
