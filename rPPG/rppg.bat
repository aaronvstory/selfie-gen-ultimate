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
python rppg_injector.py %* --inject --iterative --iterate-from-baseline --skip-diagnosis

endlocal
REM Codex P1 (2026-05-21): suppress pause when launched via Python
REM subprocess (KLING_NO_PAUSE=1 set by the caller) so the subprocess
REM doesn't hang waiting for keypress. Manual double-click still pauses.
if not defined KLING_NO_PAUSE pause