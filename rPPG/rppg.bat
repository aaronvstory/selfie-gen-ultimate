@echo off
setlocal
REM Activate the virtual environment
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
) else if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

REM Run the Python script with arguments
echo Running rppg_injector.py with arguments: %*
python rppg_injector.py %* --inject --iterative --iterate-from-base --skip-diagnosis

endlocal
pause