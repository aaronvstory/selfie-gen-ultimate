@echo off
rem Compat wrapper -> launchers\windows\run_gui_step3_v2.bat
rem Step-3 Layout v2 preview (rPPG frame gets its own re-run buttons).
rem Delegates to the canonical GUI launcher with the layout flag set.
call "%~dp0launchers\windows\run_gui_step3_v2.bat" %*
exit /b %ERRORLEVEL%
