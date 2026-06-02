@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  Kling UI - Build Distributable Executable
::  Creates dist\KlingUI\KlingUI.exe  +  dist\KlingUI.zip
::
::  Requirements: Python 3.8+
::  Run this file from its own directory.
:: ============================================================

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

echo.
echo ================================================
echo   Kling UI  ^|  Build Script
echo ================================================
echo.

:: -------- Step 1: Verify Python --------
echo [1/6] Checking Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ERROR: Python not found in PATH.
    echo Install Python 3.8+ from https://python.org and try again.
    goto :fail
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo       Found: %PYVER%

:: -------- Step 2: Install / Upgrade Dependencies --------
echo.
echo [2/6] Installing dependencies...
set "CONSTRAINTS_FILE=%SCRIPT_DIR%constraints.txt"
set "CC="
if exist "%CONSTRAINTS_FILE%" set "CC=-c "%CONSTRAINTS_FILE%""
set "MEDIAPIPE_SPEC=mediapipe==0.10.35"
python -m pip install --quiet --upgrade pyinstaller
rem -c constraints.txt: hold numpy<2 / opencv<4.12 even though requirements
rem pulls mediapipe (whose deps would otherwise float numpy to 2.x and break
rem TF 2.16.2 in the frozen build). v2.17: this install previously had no -c.
python -m pip install --quiet --upgrade !CC! -r requirements.txt
if %errorlevel% neq 0 (
    echo ERROR: pip install failed. See messages above.
    goto :fail
)
rem Re-pin mediapipe --no-deps so the exact wheel lands without its deps
rem re-resolving numpy (matches the launcher mediapipe --no-deps contract).
python -m pip install --quiet --force-reinstall --no-deps !CC! %MEDIAPIPE_SPEC%
if %errorlevel% neq 0 (
    echo ERROR: mediapipe --no-deps install failed. See messages above.
    goto :fail
)
echo       Done.

:: -------- Step 3: Generate Icon --------
echo.
echo [3/6] Generating app icon...
python create_icon.py
if %errorlevel% neq 0 (
    echo WARNING: Icon generation failed. Build will continue without icon.
)
if not exist "kling_ui.ico" (
    echo WARNING: kling_ui.ico not found. Build will continue without icon.
)

:: -------- Step 4: Clean Previous Build --------
echo.
echo [4/6] Cleaning previous build artefacts...
if exist "build"           rmdir /s /q "build"
if exist "dist\KlingUI"    rmdir /s /q "dist\KlingUI"
if exist "dist\KlingUI.zip" del /q "dist\KlingUI.zip"
echo       Done.

:: -------- Step 5: PyInstaller --------
echo.
echo [5/6] Running PyInstaller...
echo       (This may take 2-5 minutes on first run)
echo.
python -m PyInstaller kling_gui_direct.spec --noconfirm
if %errorlevel% neq 0 (
    echo.
    echo ================================================
    echo   PYINSTALLER FAILED
    echo ================================================
    echo Check the output above for errors.
    echo Common fixes:
    echo   - Ensure all imports in the spec hiddenimports list exist
    echo   - Delete __pycache__ folders and retry
    goto :fail
)

:: Verify the exe was created
if not exist "dist\KlingUI\KlingUI.exe" (
    echo.
    echo ERROR: dist\KlingUI\KlingUI.exe was not created.
    echo Check PyInstaller output above.
    goto :fail
)

:: -------- Step 6: Package into ZIP --------
echo.
echo [6/6] Creating distribution ZIP...

:: Write README into the dist folder
(
echo Kling UI - AI Video Generator
echo ================================
echo.
echo Double-click KlingUI.exe to launch.
echo.
echo FIRST RUN:
echo   You will be prompted to enter your fal.ai API key.
echo   Get your key at: https://fal.ai/dashboard/keys
echo.
echo USAGE:
echo   - Drag and drop images onto the drop zone
echo   - Right-click the drop zone to process a whole folder
echo   - Configure model, prompt, and output folder in the settings panel
echo   - Click "Start Queue" to begin generating videos
echo.
echo NOTES:
echo   - Configuration is saved in kling_config.json (next to the exe)
echo   - Logs are saved in kling_gui.log
echo   - If the app crashes, check crash_log.txt for details
echo.
echo SYSTEM REQUIREMENTS:
echo   - Windows 10 or later (64-bit)
echo   - Internet connection required
echo   - No Python installation needed - fully self-contained
echo.
echo Version: 2026-03-02
) > "dist\KlingUI\README.txt"

:: Use PowerShell to create the ZIP (available on all modern Windows)
powershell -NoProfile -Command ^
    "Compress-Archive -Path 'dist\KlingUI' -DestinationPath 'dist\KlingUI.zip' -Force"
if %errorlevel% neq 0 (
    echo WARNING: ZIP creation failed. The folder dist\KlingUI\ is still usable.
) else (
    echo       ZIP created: dist\KlingUI.zip
)

:: -------- Done --------
echo.
echo ================================================
echo   BUILD SUCCESSFUL
echo ================================================
echo.
echo Executable:  dist\KlingUI\KlingUI.exe
if exist "dist\KlingUI.zip" (
    echo   ZIP file:    dist\KlingUI.zip
    echo   ^> Share the ZIP with your colleagues.
    echo   ^> They just unzip and double-click KlingUI.exe
)
echo.
echo Opening output folder...
explorer "dist\KlingUI"

pause
endlocal
exit /b 0

:fail
echo.
echo Build failed. See messages above.
pause
endlocal
exit /b 1
