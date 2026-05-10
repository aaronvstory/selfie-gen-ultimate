@echo off
setlocal
for %%I in ("%~dp0..") do set "ROOT_DIR=%%~fI"
call "%ROOT_DIR%\launchers\windows\run_gui.bat" %*
set "EXIT_CODE=%ERRORLEVEL%"
endlocal & exit /b %EXIT_CODE%
