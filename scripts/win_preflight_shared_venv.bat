@echo off
rem ===========================================================================
rem  win_preflight_shared_venv.bat  (v2.17)
rem  Shared preflight for sub-project launchers (oldcam / similarity /
rem  resemble). CALLed (not invoked) so it inherits the caller's env.
rem
rem  Contract (review feedback 2026-06-02, 'Gipps'): a sub-launcher must NOT
rem  trust a shared root venv blindly + must NOT install its own divergent
rem  subset over a half-complete venv. Before the sub-launcher does its own
rem  minimal install, this runs the CANONICAL full-set health probe against
rem  the shared venv and repairs it if incomplete -- so a missing
rem  scipy/absl/mediapipe/torch surfaces here as one canonical repair, not
rem  later as a weird ImportError deep in oldcam/similarity.
rem
rem  Args:  %~1 = python exe (the resolved shared venv python)
rem         %~2 = REPO_ROOT (repo root holding dependency_health_check.py)
rem  Best-effort: NEVER fails the caller. A broken probe/repair just logs;
rem  the sub-launcher's own minimal import-gate is the final safety net.
rem  Opt-out: set SELFIEGEN_SKIP_PREFLIGHT=1 (e.g. for fast repeat launches).
rem ===========================================================================
set "_PF_PY=%~1"
set "_PF_ROOT=%~2"
if "%SELFIEGEN_SKIP_PREFLIGHT%"=="1" goto :_pf_done
if "%_PF_PY%"=="" goto :_pf_done
if "%_PF_ROOT%"=="" goto :_pf_done
if not exist "%_PF_ROOT%\dependency_health_check.py" goto :_pf_done
rem v2.17 (Codex P2): only repair the SHARED root venv. If the caller
rem resolved a SELFIEGEN_PYTHON override / SELFIEGEN_VENV_DIR / a local
rem fallback venv, do NOT force-reinstall the full TF/MediaPipe stack into
rem it -- the user may keep that env minimal on purpose. Match the python
rem path against the canonical shared-venv locations under REPO_ROOT; skip
rem the preflight otherwise (the sub-launcher's own minimal install +
rem import-gate remain the safety net).
set "_PF_SHARED="
if /I "%_PF_PY%"=="%_PF_ROOT%\venv\Scripts\python.exe" set "_PF_SHARED=1"
if /I "%_PF_PY%"=="%_PF_ROOT%\.venv311\Scripts\python.exe" set "_PF_SHARED=1"
if /I "%_PF_PY%"=="%_PF_ROOT%\.venv\Scripts\python.exe" set "_PF_SHARED=1"
if not defined _PF_SHARED goto :_pf_done
set "_PF_HEALTH=%_PF_ROOT%\dependency_health_check.py"
set "_PF_STATE=%_PF_ROOT%\.launcher_state"
if not exist "%_PF_STATE%\" mkdir "%_PF_STATE%" >nul 2>&1
rem Quick probe of the FULL runtime set against the shared venv.
rem NOTE (code-review M2): this helper is CALLed (no setlocal/delayed expansion),
rem so it reads %errorlevel% directly and uses the CALLed-subroutine idiom
rem "if not errorlevel 1" (= exit code is 0). Do NOT wrap the probe in a
rem parenthesized block without first adding setlocal enabledelayedexpansion +
rem switching to !errorlevel! -- %errorlevel% is not re-read inside ( ) blocks.
"%_PF_PY%" "%_PF_HEALTH%" --mode check >"%_PF_STATE%\preflight_health.log" 2>&1
if not errorlevel 1 goto :_pf_done
echo   [preflight] shared venv incomplete/broken -- running canonical repair...
echo   [preflight] see %_PF_STATE%\preflight_health.log
"%_PF_PY%" "%_PF_HEALTH%" --mode repair
if errorlevel 1 (
    echo   [preflight] WARNING: canonical repair did not fully succeed.
    echo   [preflight] The sub-app may still launch on its own minimal deps;
    echo   [preflight] if it fails, inspect %_PF_STATE%\preflight_health.log
    echo   [preflight] or delete %_PF_ROOT%\venv and relaunch the MAIN app first.
)
:_pf_done
set "_PF_PY="
set "_PF_ROOT="
set "_PF_HEALTH="
set "_PF_STATE="
set "_PF_SHARED="
goto :eof
