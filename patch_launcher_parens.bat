@echo off
setlocal
rem ============================================================
rem  Ultimate-Selfie-Gen  --  v2.17 launcher paren-crash PATCHER
rem ------------------------------------------------------------
rem  Fixes the cmd parse crash ^(quote^) <word> was unexpected at
rem  this time. ^(unquote^) that made rPPG ^(and some oldcam /
rem  similarity launchers^) fail with -NORPPG / exit 255.
rem  Run this ONCE inside your install folder. Idempotent: safe
rem  to run again, does nothing if already patched. No re-download.
rem ============================================================
set "ROOT_DIR=%~dp0"
if "%ROOT_DIR:~-1%"=="\" set "ROOT_DIR=%ROOT_DIR:~0,-1%"
set "PATCHER=%ROOT_DIR%\scripts\patch_launcher_parens.py"

echo.
echo  Patching launchers under: %ROOT_DIR%
echo.

rem --- find a python: venv first, then .venv311/.venv, then py launcher, then PATH
set "PYEXE="
if exist "%ROOT_DIR%\venv\Scripts\python.exe" set "PYEXE=%ROOT_DIR%\venv\Scripts\python.exe"
if not defined PYEXE if exist "%ROOT_DIR%\.venv311\Scripts\python.exe" set "PYEXE=%ROOT_DIR%\.venv311\Scripts\python.exe"
if not defined PYEXE if exist "%ROOT_DIR%\.venv\Scripts\python.exe" set "PYEXE=%ROOT_DIR%\.venv\Scripts\python.exe"
if not defined PYEXE (
  for %%P in (py.exe) do if not defined PYEXE set "PYEXE=py"
)
if not defined PYEXE (
  where python >nul 2>&1
  if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
  echo  ERROR: Could not find Python to run the patcher.
  echo  Open the app once ^(which creates the venv^), then re-run this.
  pause
  exit /b 1
)

if not exist "%PATCHER%" (
  echo  ERROR: Missing %PATCHER%
  echo  Make sure this .bat is in the SAME folder that contains the
  echo  scripts\ folder ^(your selfie-gen-ultimate root^).
  pause
  exit /b 1
)

"%PYEXE%" "%PATCHER%" "%ROOT_DIR%"
set "RC=%ERRORLEVEL%"
echo.
if "%RC%"=="0" (
  echo  Patch complete. You can now run rPPG normally.
) else (
  echo  Patch reported an error ^(code %RC%^). See messages above.
)
echo.
pause
endlocal & exit /b %RC%
