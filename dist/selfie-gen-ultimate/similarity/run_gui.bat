@echo off
setlocal

cd /d "%~dp0"

set "LOG_FILE=%CD%\launcher_runtime.log"
>> "%LOG_FILE%" echo.
>> "%LOG_FILE%" echo ===============================================================================
>> "%LOG_FILE%" echo [INFO] [%date% %time%] Starting run_gui.bat in %CD%

set "TF_USE_LEGACY_KERAS=1"
set "KERAS_BACKEND=tensorflow"

set "PYTHON_BIN="
if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe -V >nul 2>&1
    if not errorlevel 1 set "PYTHON_BIN=.venv\Scripts\python.exe"
)
if "%PYTHON_BIN%"=="" (
    for %%V in (3.12 3.11 3.10 3.9) do (
        if "%PYTHON_BIN%"=="" (
            py -%%V -V >nul 2>&1
            if not errorlevel 1 set "PYTHON_BIN=py -%%V"
        )
    )
)
if "%PYTHON_BIN%"=="" (
    python -V >nul 2>&1
    if not errorlevel 1 set "PYTHON_BIN=python"
)
if "%PYTHON_BIN%"=="" (
    echo [ERROR] No Python interpreter found.
    >> "%LOG_FILE%" echo [ERROR] No Python interpreter found.
    if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" pause
    exit /b 1
)

echo [INFO] Using Python interpreter: %PYTHON_BIN%
>> "%LOG_FILE%" echo [INFO] Using Python interpreter: %PYTHON_BIN%

if not exist ".venv\Scripts\activate.bat" (
    echo [INFO] Creating virtual environment...
    >> "%LOG_FILE%" echo [INFO] Creating virtual environment...
    %PYTHON_BIN% -m venv .venv >> "%LOG_FILE%" 2>&1
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        >> "%LOG_FILE%" echo [ERROR] Failed to create virtual environment.
        if "%SIMILARITY_LAUNCHED_BY_MAIN%"=="" pause
        exit /b 1
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
