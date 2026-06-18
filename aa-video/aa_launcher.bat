@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem AA (adversarial-attack) video toolkit launcher -- Windows.
rem
rem UNLIKE rPPG/oldcam (which share the main repo venv), this subproject runs
rem in its OWN ISOLATED uv venv (aa-video\.venv) because its deps (numpy 2.x,
rem opencv 4.x, optional torch) conflict with the main repo invariant
rem (numpy<2 / opencv<4.12). The launcher OWNS that venv via uv sync, then runs
rem main.py with whatever args were passed. Standalone-runnable.
rem
rem KLING_NO_PAUSE gate: when invoked from the Python subprocess (queue/
rem pipeline) the pause statements must NOT block on hidden stdin; manual
rem double-click users still get the pauses.
set "PAUSE=pause"
if defined KLING_NO_PAUSE set "PAUSE=rem skip_pause"

set "REPO_ROOT="
for %%I in ("..") do set "REPO_ROOT=%%~fI"
set "STATE_DIR=%REPO_ROOT%\.launcher_state"
if not exist "%STATE_DIR%" mkdir "%STATE_DIR%" >nul 2>&1
set "LOG_FILE=%STATE_DIR%\aa.log"

set "PYTHONUNBUFFERED=1"
set "MPLBACKEND=Agg"

rem --- Resolve uv (stdlib-only bootstrap; runs before any venv exists) ---
set "UV_BIN="
where uv >nul 2>&1 && for /f "delims=" %%U in ('where uv') do if not defined UV_BIN set "UV_BIN=%%U"
if not defined UV_BIN if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV_BIN=%USERPROFILE%\.local\bin\uv.exe"
if not defined UV_BIN call :resolve_uv_via_python
if not defined UV_BIN (
  echo   ERROR: uv not found and could not be bootstrapped.
  echo   Install uv: https://docs.astral.sh/uv/  ^(or run scripts\ensure_uv.py^)
  >>"%LOG_FILE%" echo [ERROR] uv unavailable; cannot provision aa-video venv.
  %PAUSE%
  exit /b 1
)
echo   uv: !UV_BIN!
>>"%LOG_FILE%" echo [INFO] using uv: !UV_BIN!

rem --- Sync the isolated venv (CPU-only default lock) ---
echo   Syncing aa-video venv ^(uv sync^)...
>>"%LOG_FILE%" echo [INFO] uv sync starting
"!UV_BIN!" sync >>"%LOG_FILE%" 2>&1
if errorlevel 1 (
  echo   ERROR: uv sync failed ^(see %LOG_FILE%^).
  >>"%LOG_FILE%" echo [ERROR] uv sync failed.
  %PAUSE%
  exit /b 1
)
echo   OK: aa-video venv ready.

rem --- Run the tool ---
echo   Launching aa-video main.py %*
>>"%LOG_FILE%" echo [INFO] Launching main.py %*
"!UV_BIN!" run --no-sync python main.py %*
set "EXIT_CODE=!ERRORLEVEL!"
echo   Finished with code !EXIT_CODE!.
>>"%LOG_FILE%" echo [INFO] Finished with code !EXIT_CODE!.
%PAUSE%
exit /b !EXIT_CODE!

:resolve_uv_via_python
for %%P in (python py python3) do (
  if not defined UV_BIN (
    for /f "delims=" %%U in ('%%P "%REPO_ROOT%\scripts\ensure_uv.py" --print-path 2^>nul') do (
      if not defined UV_BIN set "UV_BIN=%%U"
    )
  )
)
exit /b 0
