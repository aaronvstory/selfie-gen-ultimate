@echo off
setlocal
rem ============================================================================
rem  RUN_SUITE - unified Selfie Gen Ultimate launcher
rem  One front door: GUI / CLI / dependency check / GPU check.
rem  Delegates to the canonical launcher chain in launchers\windows (which owns
rem  Python resolution, venv bootstrap and ALL dependency installs - this file
rem  must never pip install anything itself).
rem  ASCII-only file on purpose: the ANSI ESC char is generated at RUNTIME via
rem  the forfiles 0x1B trick. Colors activate when the console is VT-capable:
rem  Windows 11 (build >= 22000, where Windows Terminal renders consoles),
rem  WT_SESSION / ConEmuANSI / ANSICON, or VirtualTerminalLevel=1. Legacy
rem  conhost gets the identical plain menu - never raw escape garbage.
rem  NOTE: color var names must avoid cmd DYNAMIC variables (CD, DATE, TIME,
rem  RANDOM, ...) - %CD% always expands to the current directory and once
rem  leaked the repo path into every "dim" slot of this menu.
rem ============================================================================

set "ROOT_DIR=%~dp0"

rem --- App version (single source: app_version.py; delims== idiom) ------------
set "APP_VER="
for /f "tokens=2 delims==" %%V in ('findstr /b /c:"RELEASE_VERSION" "%ROOT_DIR%app_version.py" 2^>nul') do set "APP_VER=%%V"
set "APP_VER=%APP_VER: =%"
set "APP_VER=%APP_VER:"=%"
if not defined APP_VER set "APP_VER=unknown"

rem --- VT capability ----------------------------------------------------------
set "VT_OK="
if defined WT_SESSION set "VT_OK=1"
if defined ConEmuANSI set "VT_OK=1"
if defined ANSICON set "VT_OK=1"
if defined VT_OK goto vt_check_done
rem Windows 11 (build >= 22000): the default terminal pipeline renders VT even
rem for plain double-clicked .bat sessions (WT_SESSION is NOT set there).
set "WIN_BUILD=0"
for /f "tokens=2 delims=[]" %%v in ('ver') do for /f "tokens=4 delims=. " %%b in ("%%v") do set "WIN_BUILD=%%b"
if %WIN_BUILD% GEQ 22000 set "VT_OK=1"
if defined VT_OK goto vt_check_done
reg query "HKCU\Console" /v VirtualTerminalLevel 2>nul | find "0x1" >nul 2>&1
if not errorlevel 1 set "VT_OK=1"
:vt_check_done
set "ESC="
if not defined VT_OK goto colors_done
for /f "delims=" %%E in ('forfiles /p "%ROOT_DIR%." /m "%~nx0" /c "cmd /c echo(0x1B" 2^>nul') do set "ESC=%%E"
if not defined ESC goto colors_done
set "CLR0=%ESC%[0m"
set "CLRB=%ESC%[1;97m"
set "CLRC=%ESC%[96m"
set "CLRY=%ESC%[93m"
set "CLRG=%ESC%[92m"
set "CLRR=%ESC%[91m"
set "CLRD=%ESC%[90m"
:colors_done

:menu
cls
echo(
echo   %CLRC%####### ####### ##      ####### ## #######     ######  ####### ###    ##%CLR0%
echo   %CLRC%##      ##      ##      ##      ## ##         ##       ##      ####   ##%CLR0%
echo   %CLRC%####### #####   ##      #####   ## #####      ##   ### #####   ## ##  ##%CLR0%
echo   %CLRC%     ## ##      ##      ##      ## ##         ##    ## ##      ##  ## ##%CLR0%
echo   %CLRC%####### ####### ####### ##      ## #######     ######  ####### ##   ####%CLR0%
echo(
echo             %CLRB%ULTIMATE  %APP_VER%%CLR0%  %CLRD%-  Front - Selfie - Similarity - Video - Oldcam%CLR0%
echo   %CLRD%==========================================================================%CLR0%
call :gpu_brief
echo(
echo     %CLRY%[1]%CLR0%  %CLRB%Launch GUI%CLR0%          %CLRD%(Tkinter manual lab)%CLR0%
echo     %CLRY%[2]%CLR0%  %CLRB%Launch CLI%CLR0%          %CLRD%(automation pipeline)%CLR0%
echo     %CLRY%[3]%CLR0%  Dependency check / repair
echo     %CLRY%[4]%CLR0%  GPU details         %CLRD%(nvidia-smi + CUDA)%CLR0%
echo     %CLRR%[Q]%CLR0%  Quit
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
echo     %CLRG%GPU:%CLR0% %GPU_NAME%  %CLRD%(driver %GPU_DRV%)%CLR0%
goto :eof
:gpu_brief_none
echo     %CLRY%GPU: none detected (nvidia-smi not found) - rPPG will run on CPU%CLR0%
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
echo   %CLRY%No venv yet - the GUI/CLI launchers create it on first run.%CLR0%
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
echo   %CLRC%--- Per-GPU summary -------------------------------------------------------%CLR0%
nvidia-smi --query-gpu=name,driver_version,memory.total,memory.used --format=csv
goto gpu_full_done
:gpu_full_none
echo   %CLRY%nvidia-smi not found - no NVIDIA GPU/driver detected. rPPG runs on CPU.%CLR0%
:gpu_full_done
echo(
pause
goto menu

:done
endlocal
exit /b 0
