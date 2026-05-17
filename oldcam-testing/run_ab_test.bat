@echo off
setlocal
rem oldcam-testing A/B harness launcher (standalone experiment).
rem Resolves the repo venv python, then runs run_ab_test.py with all args.
set "SCRIPT_DIR=%~dp0"
set "REPO_ROOT=%SCRIPT_DIR%.."
set "PYEXE="
if exist "%REPO_ROOT%\venv\Scripts\python.exe" set "PYEXE=%REPO_ROOT%\venv\Scripts\python.exe"
if not defined PYEXE if exist "%REPO_ROOT%\.venv\Scripts\python.exe" set "PYEXE=%REPO_ROOT%\.venv\Scripts\python.exe"
if not defined PYEXE if exist "%REPO_ROOT%\.venv311\Scripts\python.exe" set "PYEXE=%REPO_ROOT%\.venv311\Scripts\python.exe"
if not defined PYEXE set "PYEXE=python"
echo [%DATE% %TIME%] oldcam-testing A/B run
echo Repo root: %REPO_ROOT%
echo Python:    %PYEXE%
if "%~1"=="" (
  echo(
  echo Usage: run_ab_test.bat "C:\path\to\kling_clip.mp4" [more.mp4 ...]
  echo        run_ab_test.bat "clip.mp4" --no-score
  exit /b 2
)
"%PYEXE%" "%SCRIPT_DIR%run_ab_test.py" %*
set "RC=%ERRORLEVEL%"
echo(
echo [%DATE% %TIME%] finished, exit code %RC%
exit /b %RC%
