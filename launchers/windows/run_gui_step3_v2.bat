@echo off
rem === Step-3 Layout v2 preview launcher (Windows) ===
rem Launches the GUI with the experimental Step-3 layout v2: the orange
rem rPPG frame gets its own re-run/file-picker buttons. The DEFAULT
rem layout is unchanged everywhere else. See docs/rppg-wiring.md.
rem
rem Delegates to the canonical GUI launcher (full venv create + Python
rem version validation + dependency bootstrap) with the layout flag set,
rem instead of duplicating a partial resolver here (Codex P2 / PR #39,
rem CLAUDE.md Hard Rule #9).
set "SELFIEGEN_STEP3_LAYOUT=v2"
call "%~dp0run_gui.bat" %*
exit /b %ERRORLEVEL%
