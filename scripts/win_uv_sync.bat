@echo off
rem ===========================================================================
rem  win_uv_sync.bat  (v2.20)
rem  Shared uv fast-path for the Windows launchers. CALLed (not invoked) so it
rem  inherits + sets the caller's env. Attempts the uv-native dependency sync
rem  (scripts\uv_sync_deps.py: ensure uv -> GPU-aware torch extra -> uv sync)
rem  and reports the outcome so the caller can skip its legacy pip block.
rem
rem  Args:  %~1 = python exe (used only to RUN the orchestrator script; uv
rem               itself provisions the project env at %ROOT%\venv)
rem         %~2 = ROOT_DIR (repo root holding uv.lock + scripts\)
rem
rem  Sets:  UV_SYNCED=1  when the uv path produced a ready env (caller skips
rem                      its pip sync and launches directly).
rem         UV_SYNCED=   (empty) when the caller must FALL BACK to pip.
rem
rem  Opt-out: set KLING_USE_PIP=1 to force the legacy pip path (this helper
rem  then no-ops with UV_SYNCED empty). Best-effort: any failure leaves
rem  UV_SYNCED empty so the caller's pip path takes over -- never blocks.
rem ===========================================================================
set "UV_SYNCED="
set "_UV_PY=%~1"
set "_UV_ROOT=%~2"
if "%KLING_USE_PIP%"=="1" goto :_uv_done
if "%_UV_PY%"=="" goto :_uv_done
if "%_UV_ROOT%"=="" goto :_uv_done
if not exist "%_UV_ROOT%\uv.lock" goto :_uv_done
if not exist "%_UV_ROOT%\scripts\uv_sync_deps.py" goto :_uv_done
echo   [uv] syncing dependencies via uv (set KLING_USE_PIP=1 to force pip)...
rem Pass --python so uv targets the SAME venv the caller resolved (not the
rem canonical default) -- avoids provisioning one env while the launcher
rem runs another (CodeRabbit Major, PR #71).
"%_UV_PY%" "%_UV_ROOT%\scripts\uv_sync_deps.py" --project "%_UV_ROOT%" --python "%_UV_PY%"
rem uv_sync_deps exit codes: 0 = env ready; 3 = fall back to pip.
if errorlevel 3 goto :_uv_done
if errorlevel 1 goto :_uv_done
set "UV_SYNCED=1"
echo   [uv] dependencies ready (uv-managed venv).
:_uv_done
set "_UV_PY="
set "_UV_ROOT="
goto :eof
