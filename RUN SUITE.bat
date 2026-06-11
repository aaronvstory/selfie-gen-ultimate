@echo off
setlocal
rem ============================================================================
rem  RUN SUITE - unified Selfie Gen Ultimate launcher
rem  One front door: GUI / CLI / dependency check / GPU check.
rem  Delegates to the canonical launcher chain in launchers\windows (which owns
rem  Python resolution, venv bootstrap and ALL dependency installs - this file
rem  must never pip install anything itself).
rem  ASCII-only file on purpose: the ANSI ESC char is generated at RUNTIME via
rem  the forfiles 0x1B trick, and only when the console supports VT (Windows
rem  Terminal / ConEmu / ANSICON / VirtualTerminalLevel=1). On legacy conhost
rem  every C_* var stays empty and the menu renders plain - zero risk.
rem ============================================================================

set "ROOT_DIR=%~dp0"

rem --- App version (single source: app_version.py; delims== idiom) ------------
set "APP_VER="
for /f "tokens=2 delims==" %%V in ('findstr /b /c:"RELEASE_VERSION" "%ROOT_DIR%app_version.py" 2^>nul') do set "APP_VER=%%V"
set "APP_VER=%APP_VER: =%"
set "APP_VER=%APP_VER:"=%"
if not defined APP_VER set "APP_VER=unknown"

rem --- ANSI palette (gated on VT-capable consoles) -----------------------------
set "VT_OK="
if defined WT_SESSION set "VT_OK=1"
if defined ConEmuANSI set "VT_OK=1"
if defined ANSICON set "VT_OK=1"
if defined VT_OK goto vt_check_done
reg query "HKCU\Console" /v VirtualTerminalLevel 2>nul | find "0x1" >nul 2>&1
if not errorlevel 1 set "VT_OK=1"
:vt_check_done
set "ESC="
if not defined VT_OK goto colors_done
for /f "delims=" %%E in ('forfiles /p "%ROOT_DIR%." /m "%~nx0" /c "cmd /c echo(0x1B" 2^>nul') do set "ESC=%%E"
if not defined ESC goto colors_done
set "C0=%ESC%[0m"
set "CB=%ESC%[1;97m"
set "CC=%ESC%[96m"
set "CM=%ESC%[95m"
set "CY=%ESC%[93m"
set "CG=%ESC%[92m"
set "CR=%ESC%[91m"
set "CD=%ESC%[90m"
:colors_done

:menu
cls
echo(
echo   %CC%####### ####### ##      ####### ## #######     ######  ####### ###    ##%C0%
echo   %CC%##      ##      ##      ##      ## ##         ##       ##      ####   ##%C0%
echo   %CC%####### #####   ##      #####   ## #####      ##   ### #####   ## ##  ##%C0%
echo   %CC%     ## ##      ##      ##      ## ##         ##    ## ##      ##  ## ##%C0%
echo   %CC%####### ####### ####### ##      ## #######     ######  ####### ##   ####%C0%
echo(
echo             %CB%ULTIMATE  %APP_VER%%C0%  %CD%-  Front - Selfie - Similarity - Video - Oldcam%C0%
echo   %CD%==========================================================================%C0%
call :gpu_brief
echo(
echo     %CY%[1]%C0%  %CB%Launch GUI%C0%          %CD%(Tkinter manual lab)%C0%
echo     %CY%[2]%C0%  %CB%Launch CLI%C0%          %CD%(automation pipeline)%C0%
echo     %CY%[3]%C0%  Dependency check / repair
echo     %CY%[4]%C0%  GPU details         %CD%(nvidia-smi + CUDA)%C0%
echo     %CR%[Q]%C0%  Quit
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
echo     %CG%GPU:%C0% %GPU_NAME%  %CD%(driver %GPU_DRV%)%C0%
goto :eof
:gpu_brief_none
echo     %CY%GPU: none detected (nvidia-smi not found) - rPPG will run on CPU%C0%
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
echo   %CY%No venv yet - the GUI/CLI launchers create it on first run.%C0%
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
echo   %CC%--- Per-GPU summary -------------------------------------------------------%C0%
nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used --format=csv
goto gpu_full_done
:gpu_full_none
echo   %CY%nvidia-smi not found - no NVIDIA GPU/driver detected. rPPG runs on CPU.%C0%
:gpu_full_done
echo(
pause
goto menu

:done
endlocal
exit /b 0
