@echo off
setlocal
rem ============================================================================
rem  RUN SUITE - unified Selfie Gen Ultimate launcher
rem  One front door: GUI / CLI / dependency check / GPU check.
rem  Delegates to the canonical launcher chain in launchers\windows (which owns
rem  Python resolution, venv bootstrap and ALL dependency installs - this file
rem  must never pip install anything itself).
rem  ASCII-only on purpose: parses safely in every cmd codepage.
rem ============================================================================

set "ROOT_DIR=%~dp0"

rem --- App version (single source: app_version.py; delims== idiom) ------------
set "APP_VER="
for /f "tokens=2 delims==" %%V in ('findstr /b /c:"RELEASE_VERSION" "%ROOT_DIR%app_version.py" 2^>nul') do set "APP_VER=%%V"
set "APP_VER=%APP_VER: =%"
set "APP_VER=%APP_VER:"=%"
if not defined APP_VER set "APP_VER=unknown"

:menu
cls
echo(
echo   ####### ####### ##      ####### ## #######     ######  ####### ###    ##
echo   ##      ##      ##      ##      ## ##         ##       ##      ####   ##
echo   ####### #####   ##      #####   ## #####      ##   ### #####   ## ##  ##
echo        ## ##      ##      ##      ## ##         ##    ## ##      ##  ## ##
echo   ####### ####### ####### ##      ## #######     ######  ####### ##   ####
echo(
echo             ULTIMATE  %APP_VER%  -  Front - Selfie - Similarity - Video - Oldcam
echo   ==========================================================================
call :gpu_brief
echo(
echo     [1]  Launch GUI          (Tkinter manual lab)
echo     [2]  Launch CLI          (automation pipeline)
echo     [3]  Dependency check / repair
echo     [4]  GPU details         (nvidia-smi + CUDA)
echo     [Q]  Quit
echo(
set "SUITE_CHOICE="
set /p "SUITE_CHOICE=   Select an option: "
if /i "%SUITE_CHOICE%"=="1" goto launch_gui
if /i "%SUITE_CHOICE%"=="2" goto launch_cli
if /i "%SUITE_CHOICE%"=="3" goto deps
if /i "%SUITE_CHOICE%"=="4" goto gpu_full
if /i "%SUITE_CHOICE%"=="q" goto done
goto menu

:gpu_brief
set "GPU_NAME="
set "GPU_DRV="
where nvidia-smi >nul 2>&1
if errorlevel 1 goto gpu_brief_none
for /f "tokens=1,2 delims=," %%A in ('cmd /c "nvidia-smi --query-gpu=name,driver_version --format=csv,noheader" 2^>nul') do set "GPU_NAME=%%A" & set "GPU_DRV=%%B"
if not defined GPU_NAME goto gpu_brief_none
set "GPU_DRV=%GPU_DRV: =%"
echo     GPU: %GPU_NAME%  (driver %GPU_DRV%)
goto :eof
:gpu_brief_none
echo     GPU: none detected (nvidia-smi not found) - rPPG will run on CPU
goto :eof

:launch_gui
call "%ROOT_DIR%launchers\windows\run_gui.bat"
goto menu

:launch_cli
call "%ROOT_DIR%launchers\windows\run_cli.bat"
goto menu

:deps
echo(
if exist "%ROOT_DIR%venv\Scripts\python.exe" goto deps_venv
echo   No venv yet - the GUI/CLI launchers create it on first run.
echo   Running the dependency checker with the system Python instead...
py -3 "%ROOT_DIR%dependency_checker.py"
if errorlevel 1 python "%ROOT_DIR%dependency_checker.py"
goto deps_done
:deps_venv
"%ROOT_DIR%venv\Scripts\python.exe" "%ROOT_DIR%dependency_checker.py"
:deps_done
echo(
pause
goto menu

:gpu_full
echo(
where nvidia-smi >nul 2>&1
if errorlevel 1 goto gpu_full_none
nvidia-smi
echo(
echo   --- Per-GPU summary -------------------------------------------------------
nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used --format=csv
goto gpu_full_done
:gpu_full_none
echo   nvidia-smi not found - no NVIDIA GPU/driver detected. rPPG runs on CPU.
:gpu_full_done
echo(
pause
goto menu

:done
endlocal
exit /b 0
