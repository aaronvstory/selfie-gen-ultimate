@echo off
setlocal
set "ROOT_DIR=%~dp0"
set "TARGET=%ROOT_DIR%launchers\run_cli.bat"

if not exist "%TARGET%" (
    echo.
    echo ERROR: Missing launcher: %TARGET%
    echo.
    if not defined KLING_NONINTERACTIVE pause
    exit /b 1
)

call "%TARGET%" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
