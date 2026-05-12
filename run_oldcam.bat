@echo off
setlocal
set "ROOT_DIR=%~dp0"
set "TARGET=%ROOT_DIR%launchers\run_oldcam.bat"
if not exist "%TARGET%" (
  echo(
  echo  ERROR: Missing launcher: %TARGET%
  echo(
  pause
  exit /b 1
)
call "%TARGET%" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
