@echo off
setlocal enabledelayedexpansion

for %%I in ("%~dp0..\..") do set "ROOT_DIR=%%~fI"
set "GUI_SCRIPT=%ROOT_DIR%\gui_launcher.py"
set "VENV_DIR=%ROOT_DIR%\venv"
set "VENV_PYTHON=%VENV_DIR%\Scripts\python.exe"
set "REQUIREMENTS=%ROOT_DIR%\requirements.txt"
set "OLDCAM_V7_REQUIREMENTS=%ROOT_DIR%\oldcam-v7\requirements.txt"
set "OLDCAM_V8_REQUIREMENTS=%ROOT_DIR%\oldcam-v8\requirements.txt"
set "OLDCAM_V9_REQUIREMENTS=%ROOT_DIR%\oldcam-v9\requirements.txt"
set "OLDCAM_V10_REQUIREMENTS=%ROOT_DIR%\oldcam-v10\requirements.txt"
set "MEDIAPIPE_SPEC=mediapipe==0.10.35"
set "DEP_CHECKER=%ROOT_DIR%\dependency_checker.py"
set "DEP_HEALTH_SCRIPT=%ROOT_DIR%\dependency_health_check.py"
set "CONSTRAINTS_FILE=%ROOT_DIR%\constraints.txt"
set "STATE_DIR=%ROOT_DIR%\.launcher_state"
set "LOG_FILE=%STATE_DIR%\launch.log"

if not exist "%STATE_DIR%\" mkdir "%STATE_DIR%"

rem --- GUI-runtime transcript setup (the LAUNCHER/install is never piped) -----
rem  The dependency install (uv/pip) runs on a real console so its live progress
rem  bars + colors render beautifully (piping through tee would kill the bars +
rem  paint benign stderr red). Only the GUI RUNTIME is tee'd -- as you USE the
rem  app, its output (rPPG processing, crashes, etc.) is written live to a
rem  rolling transcript-<ts>.log under .launcher_state\ so you can hand over one
rem  file. Compute the filename here; the GUI-launch step below tees to it.
rem  Opt-out: set KLING_NO_TRANSCRIPT=1 (subprocess callers set this).
set "TRANSCRIPT_FILE="
if defined KLING_NO_TRANSCRIPT goto :transcript_setup_done
for /f "tokens=1-2 delims==" %%A in ('wmic os get LocalDateTime /value 2^>nul') do if "%%A"=="LocalDateTime" set "TEE_DT=%%B"
set "TEE_DT=%TEE_DT: =%"
if "%TEE_DT%"=="" set "TEE_STAMP=run"
if not "%TEE_DT%"=="" set "TEE_STAMP=%TEE_DT:~0,8%-%TEE_DT:~8,6%"
set "TRANSCRIPT_FILE=%STATE_DIR%\transcript-%TEE_STAMP%.log"
:transcript_setup_done

rem --- Timestamp banner -----------------------------------------------------
for /f "tokens=1-2 delims==" %%A in ('wmic os get LocalDateTime /value 2^>nul') do if "%%A"=="LocalDateTime" set "WMIC_DT=%%B"
set "WMIC_DT=%WMIC_DT: =%"
set "LAUNCH_TS=%WMIC_DT:~0,4%-%WMIC_DT:~4,2%-%WMIC_DT:~6,2% %WMIC_DT:~8,2%:%WMIC_DT:~10,2%:%WMIC_DT:~12,2%"
rem --- Release version (parsed from app_version.py text; no Python needed,
rem --- so it prints even on a truly fresh system before the venv exists).
rem --- Single source of truth: app_version.RELEASE_VERSION -- same constant
rem --- the GUI chip and the release-zip name read, so this auto-tracks builds.
rem --- Parse the line "RELEASE_VERSION = "vX.Y"": take the value after =,
rem --- then strip spaces and quotes. Using delims== (not the fragile
rem --- delims=^" which cmd mis-parses inside a quoted options string).
set "APP_VER="
for /f "tokens=2 delims==" %%V in ('findstr /b /c:^"RELEASE_VERSION^" ^"%ROOT_DIR%\app_version.py^" 2^>nul') do set "APP_VER=%%V"
set "APP_VER=%APP_VER: =%"
set "APP_VER=%APP_VER:"=%"
if not defined APP_VER set "APP_VER=unknown"
echo(
echo  ============================================================
echo   Ultimate-Selfie-Gen  %APP_VER%  --  GUI Launcher
echo  ============================================================
echo   [%LAUNCH_TS%] Launch started  ^(version %APP_VER%^)
echo   Root: %ROOT_DIR%
echo(
>>"%LOG_FILE%" echo [%LAUNCH_TS%] app version %APP_VER%
>>"%LOG_FILE%" echo [%LAUNCH_TS%] GUI launch started

rem --- PR #49: bootstrap mutex (concurrent launches must not race pip) -
set "SETUP_LOCK=%STATE_DIR%\setup.lock"
:acquire_setup_lock
md "%SETUP_LOCK%" >nul 2>&1
if !errorlevel! equ 0 goto :setup_lock_acquired
rem Stale-lock check: -d -1 = older than 1 day. Conservative; the .sh
rem path uses a finer 10-min window. Bootstrap should never take >24h.
forfiles /P "%STATE_DIR%" /M setup.lock /D -1 >nul 2>&1
if !errorlevel! equ 0 (
    echo   [setup-lock] removing stale lock
    rd /S /Q "%SETUP_LOCK%" >nul 2>&1
    goto :acquire_setup_lock
)
if not defined SETUP_LOCK_WAIT_LOGGED (
    echo   [setup-lock] another launcher is running dependency setup; waiting...
    set "SETUP_LOCK_WAIT_LOGGED=1"
)
rem Sleep 2s. ping is universally available; timeout needs interactive console.
ping -n 3 127.0.0.1 >nul 2>&1
goto :acquire_setup_lock
:setup_lock_acquired

rem --- Resolve a Python interpreter + ensure the venv exists ----------------
rem  All detection (existing venvs, py launcher, PATH, common install dirs)
rem  AND silent auto-install of Python 3.12 (winget -> python.org) lives in
rem  the shared resolver so every Windows launcher behaves identically. The
rem  py launcher (py -3.11/-3.12) is the key fix: it finds an interpreter the
rem  user installed but never added to PATH -- the failure that motivated
rem  this. The resolver is CALLED (not run with its own setlocal) so it sets
rem  VENV_PYTHON + RESOLVE_RC in THIS environment.
if not exist "%VENV_PYTHON%" (
    call "%ROOT_DIR%\scripts\win_resolve_python.bat"
    if not "!RESOLVE_RC!"=="0" (
        echo(
        echo  ERROR: Could not resolve or install a supported Python ^(3.9-3.12^).
        echo  See the messages above and %LOG_FILE% for details.
        echo(
        >>"%LOG_FILE%" echo [%LAUNCH_TS%] ERROR: python resolver failed ^(RESOLVE_RC=!RESOLVE_RC!^)
        call :release_setup_lock
        pause
        exit /b 1
    )
    rem  Resolver may have adopted/created a different venv than the
    rem  caller's %VENV_PYTHON% guess. Clear the dep stamp INSIDE this
    rem  block so the sync re-runs against the resolved venv -- only when
    rem  the resolver actually ran (venv was missing). Unconditional
    rem  deletion would nuke the stamp every launch + defeat the cache
    rem  (gemini/codex CRITICAL, bot round 2).
    if not "%STATE_DIR%"=="" del "%STATE_DIR%\deps_*.ok" >nul 2>&1
)
rem --- Per-launch diagnostic snapshot ---------------------------------------
rem Writes Python / pip / OS / GPU info to the launch log so users have
rem something to attach when reporting issues. The user's explicit ask:
rem "ensure we get proper logging each launch so we can diagnose issues
rem easier." Defensive: python invocations are gated on the venv
rem existing, the nvidia-smi block is gated on `where nvidia-smi`
rem returning 0, and `ver` is always present on Windows.
rem
rem Multi-GPU machines: only the FIRST GPU line is captured (the
rem `if "!DIAG_GPU!"=="no-nvidia-smi"` guard prevents subsequent
rem matches from clobbering). For the friend-bug scenario, knowing
rem at-least-one GPU is present is enough to disambiguate
rem "missing nvidia-smi" vs "CUDA install in flight"; full multi-GPU
rem enumeration would be a deferred feature if anyone asks.
set "DIAG_PY=unknown"
set "DIAG_PIP=unknown"
set "DIAG_OS=unknown"
set "DIAG_GPU=no-nvidia-smi"
if exist "%VENV_PYTHON%" (
    rem Caret-escaped quotes (`^"`) inside `for /f ('...')` are the safer
    rem idiom for paths containing spaces. The previous form `""%VAR%"`
    rem (double-double-quote) is a cmd parser trick that works in most
    rem cases but Gemini PR #55 round-2 MED flagged it as fragile against
    rem paths with spaces. Switching to `^"` is the standard documented
    rem form and matches what cmd's own docs recommend for nested quotes
    rem in for-command-strings.
    for /f "delims=" %%V in ('^"%VENV_PYTHON%^" -V 2^>^&1') do set "DIAG_PY=%%V"
    for /f "delims=" %%V in ('^"%VENV_PYTHON%^" -m pip --version 2^>^&1') do set "DIAG_PIP=%%V"
)
for /f "delims=" %%V in ('ver ^| findstr /R "."') do set "DIAG_OS=%%V"
where nvidia-smi >nul 2>&1
if !errorlevel! equ 0 (
    for /f "delims=" %%G in ('nvidia-smi -L 2^>^&1') do (
        if "!DIAG_GPU!"=="no-nvidia-smi" set "DIAG_GPU=%%G"
    )
)
>>"%LOG_FILE%" echo [%LAUNCH_TS%] diag-py %DIAG_PY%
>>"%LOG_FILE%" echo [%LAUNCH_TS%] diag-pip %DIAG_PIP%
>>"%LOG_FILE%" echo [%LAUNCH_TS%] diag-os %DIAG_OS%
>>"%LOG_FILE%" echo [%LAUNCH_TS%] diag-gpu %DIAG_GPU%

rem --- Build stamp key from req file dates+sizes (no subprocess needed) -----
set "STAMP_KEY="
for %%F in ("%REQUIREMENTS%" "%OLDCAM_V7_REQUIREMENTS%" "%OLDCAM_V8_REQUIREMENTS%" "%OLDCAM_V9_REQUIREMENTS%" "%OLDCAM_V10_REQUIREMENTS%") do (
    if exist "%%~F" set "STAMP_KEY=!STAMP_KEY!%%~tF%%~zF"
)
set "STAMP_KEY=%STAMP_KEY: =_%"
set "STAMP_KEY=%STAMP_KEY:/=-%"
set "STAMP_KEY=%STAMP_KEY::=-%"
rem --- v2.17: fold in the installer/GPU-mode/constraints token so the dep
rem --- stamp invalidates when the installer logic bumps, the user adds/
rem --- removes a GPU, or constraints.txt changes. The for/f wraps the
rem --- quoted python path in `cmd /c "..."` (a bare caret-quoted first
rem --- token captures NOTHING and the fold-in silently no-ops).
set "GPU_STAMP_TOKEN="
if exist "%ROOT_DIR%\scripts\gpu_bootstrap.py" (
    for /f "usebackq delims=" %%T in (`cmd /c ""%VENV_PYTHON%" "%ROOT_DIR%\scripts\gpu_bootstrap.py" --print-stamp-token"`) do set "GPU_STAMP_TOKEN=%%T"
)
set "STAMP_KEY=%STAMP_KEY%!GPU_STAMP_TOKEN!"
set "STAMP_KEY=%STAMP_KEY: =_%"
set "STAMP_KEY=%STAMP_KEY:/=-%"
set "STAMP_KEY=%STAMP_KEY::=-%"
set "STAMP_KEY=%STAMP_KEY:.=-%"
set "STAMP=%STATE_DIR%\deps_%STAMP_KEY:~0,72%.ok"

rem --- Stamp present? Skip the expensive pip-install sync, but STILL run a
rem --- runtime health check on every launch and auto-repair if it fails.
rem ---
rem --- Background: PR fix/windows-tf-health-check addressed a user report
rem --- where a friend ran run_gui.bat once, got a successful CUDA install,
rem --- then saw "RetinaFace/TensorFlow import failed. Run run_gui.bat for
rem --- automatic dependency repair." in the GUI. Re-running run_gui.bat
rem --- did nothing because the previous "successful" install wrote
rem --- deps_*.ok and the launcher skipped EVERY check on the next pass -
rem --- including the health probe that would have caught the broken
rem --- TF/retinaface stack. The user was stuck in an infinite "re-run the
rem --- bat" loop with no recovery path. Now we always probe runtime
rem --- health (~3-5s) and if it fails, we clear the stamp, run
rem --- `--mode repair` (which itself does verify_in_fresh_process), and
rem --- bubble up a CLEAR diagnostic on persistent failure instead of
rem --- telling users to "re-run run_gui.bat".
if exist "%STAMP%" (
    if exist "%DEP_HEALTH_SCRIPT%" (
        echo   [%LAUNCH_TS%] Cached deps stamp present -- running quick health probe...
        >>"%LOG_FILE%" echo [%LAUNCH_TS%] health-probe START ^(cached-stamp path^)
        "%VENV_PYTHON%" "%DEP_HEALTH_SCRIPT%" --mode check >"%STATE_DIR%\last_health.log" 2>&1
        if !errorlevel! neq 0 (
            echo(
            echo   [%LAUNCH_TS%] Runtime health probe FAILED. Recent output:
            type "%STATE_DIR%\last_health.log"
            echo(
            >>"%LOG_FILE%" echo [%LAUNCH_TS%] health-probe FAIL ^(cached-stamp path^); clearing stamp + running repair
            echo   [%LAUNCH_TS%] Clearing cached deps stamp + running auto-repair...
            del "%STATE_DIR%\deps_*.ok" >nul 2>&1
            "%VENV_PYTHON%" "%DEP_HEALTH_SCRIPT%" --mode repair
            if !errorlevel! neq 0 (
                echo(
                echo  ============================================================
                echo  ERROR: Automatic dependency repair FAILED.
                echo  ============================================================
                echo  The cached install is broken AND the auto-repair did not
                echo  fix it. Re-running %~nx0 alone will not help -- the stamp
                echo  has already been cleared, so the next run will retry the
                echo  full install, but if pip can't resolve the conflict on
                echo  its own you need to recover manually:
                echo(
                echo    1. Delete the venv folder ^(rd /S /Q "%VENV_DIR%"^) and
                echo       run %~nx0 from a clean state.
                echo    2. Force-reinstall the face stack manually ^(mirrors
                echo       REPAIR_PACKAGES in dependency_health_check.py^):
                rem  Inside () blocks cmd parses each line twice. ^^ collapses
                rem  to ^ during parse-1, then execute-1 sees `echo ... ^` at
                rem  end-of-line and treats the surviving ^ as line-cont,
                rem  printing nothing -- the user would see a broken multi-
                rem  line. ^^^^ collapses to ^^ during parse-1, then echo
                rem  prints a literal ^ to the screen for copy-paste. (Gemini
                rem  PR #55 round 2 HIGH.)
                echo       "%VENV_PYTHON%" -m pip install --force-reinstall ^^^^
                echo         --no-cache-dir numpy==1.26.4 tensorflow==2.16.2 ^^^^
                echo         tensorflow-intel==2.16.2 protobuf==4.25.3 ^^^^
                echo         tf-keras==2.16.0 retina-face==0.0.17 ^^^^
                echo         deepface==0.0.92 ^^^^
                echo         scipy^>=1.11,^<2 absl-py^>=2.3,^<3
                echo    3. Inspect the diagnostic log at:
                echo       %STATE_DIR%\last_health.log
                echo    4. Inspect the launch log at:
                echo       %LOG_FILE%
                echo(
                >>"%LOG_FILE%" echo [%LAUNCH_TS%] health-repair FAIL ^(cached-stamp path^); exiting
                call :release_setup_lock
                pause
                exit /b 1
            )
            echo   [%LAUNCH_TS%] Repair succeeded; re-writing stamp.
            >>"%STAMP%" echo %LAUNCH_TS% repair
            >>"%LOG_FILE%" echo [%LAUNCH_TS%] health-repair OK ^(cached-stamp path^); stamp re-written
        ) else (
            echo   [%LAUNCH_TS%] Runtime health: OK ^(cached deps^).
            >>"%LOG_FILE%" echo [%LAUNCH_TS%] health-probe OK ^(cached-stamp path^)
        )
    ) else (
        echo   [%LAUNCH_TS%] Dependencies up-to-date ^(cached stamp; no health script^).
    )
    echo   Tip: delete .launcher_state\deps_*.ok to force a full re-sync.
    echo(
    call :release_setup_lock
    goto :launch
)

rem --- Full dep sync (requirements changed or first run) -------------------
echo   [%LAUNCH_TS%] Requirements changed -- syncing dependencies...
echo(
rem --- Heavy-install user banner. Explains the 5-15 min wait so users
rem --- don't kill the process thinking it's frozen. Real-world data
rem --- point: a friend on Windows nvidia killed run_gui.bat at ~10 min
rem --- during the CUDA wheel download (~2GB), leaving a half-installed
rem --- state that then triggered the in-GUI "RetinaFace import failed"
rem --- toast that this PR was opened to fix. Set expectations up front.
echo  ============================================================
echo   FIRST-RUN DEP INSTALL -- expect 5 to 15 minutes
echo  ============================================================
echo   - torch wheels: ~2GB on Windows nvidia ^(CUDA-aware build^)
echo   - tensorflow + mediapipe + opencv: ~1-2GB more
echo   - subsequent launches skip this entire block ^(cached stamp^)
echo   - pip will print progress below; if 60+ sec of silence,
echo     check your network or Ctrl+C and re-run %~nx0.
echo  ============================================================
echo(
>>"%LOG_FILE%" echo [%LAUNCH_TS%] dep-install banner shown ^(full-sync path^)
"%VENV_PYTHON%" -m pip install --upgrade pip >nul 2>&1
call :INSTALL_REQUIREMENTS "%REQUIREMENTS%" "base"
if !errorlevel! neq 0 goto :BASE_DEP_FAIL

for %%R in ("%OLDCAM_V7_REQUIREMENTS%" "%OLDCAM_V8_REQUIREMENTS%" "%OLDCAM_V9_REQUIREMENTS%" "%OLDCAM_V10_REQUIREMENTS%") do if exist "%%~R" (
    echo(
    call :INSTALL_REQUIREMENTS "%%~R" "oldcam"
    if !errorlevel! neq 0 goto :DEPENDENCY_FAIL
)

rem --- v2.17: select the hardware-appropriate torch wheel. The -r install
rem --- above landed the default CPU/PyPI torch wheel; this detects NVIDIA
rem --- and reinstalls the CUDA build when present (macOS path never runs
rem --- this .bat; on Windows no-NVIDIA it is a no-op CPU reinstall). It
rem --- probes torch.cuda.is_available() and falls back to CPU torch if a
rem --- CUDA build is runtime-broken. Best-effort: always exits 0, never
rem --- blocks launch (torch only affects similarity anti-spoofing speed).
if exist "%ROOT_DIR%\scripts\gpu_bootstrap.py" (
    "%VENV_PYTHON%" "%ROOT_DIR%\scripts\gpu_bootstrap.py" --select-torch "torch>=2.2,<3" --constraints "%CONSTRAINTS_FILE%"
)

echo(
echo   [%LAUNCH_TS%] Dependency sync complete.
echo(

set "HEALTH_OK="
if exist "%DEP_HEALTH_SCRIPT%" (
    if exist "%DEP_CHECKER%" (
        echo   [%LAUNCH_TS%] Running dependency bootstrap...
        "%VENV_PYTHON%" "%DEP_CHECKER%" --auto --enforce-all
        if !errorlevel! neq 0 (
            echo(
            echo  ERROR: Dependency bootstrap failed.
            echo(
            call :release_setup_lock
            pause
            exit /b 1
        )
    )

    echo   [%LAUNCH_TS%] Validating runtime dependency health...
    >>"%LOG_FILE%" echo [%LAUNCH_TS%] health-probe START ^(fresh-install path^)
    "%VENV_PYTHON%" "%DEP_HEALTH_SCRIPT%" --mode check >"%STATE_DIR%\last_health.log" 2>&1
    if !errorlevel! neq 0 (
        echo(
        echo   [%LAUNCH_TS%] Health check FAILED. Recent output:
        type "%STATE_DIR%\last_health.log"
        echo(
        >>"%LOG_FILE%" echo [%LAUNCH_TS%] health-probe FAIL ^(fresh-install path^); attempting repair
        echo   [%LAUNCH_TS%] Attempting auto-repair...
        "%VENV_PYTHON%" "%DEP_HEALTH_SCRIPT%" --mode repair
        if !errorlevel! neq 0 (
            echo(
            echo  ============================================================
            echo  ERROR: Automatic dependency repair FAILED ^(fresh install^).
            echo  ============================================================
            echo  The pip-install sync just completed BUT the runtime health
            echo  check still failed AND auto-repair could not fix it. This
            echo  usually means a CUDA/CPU TensorFlow conflict, a partially
            echo  downloaded wheel from a flaky connection, or an antivirus
            echo  quarantine on TF DLLs.
            echo(
            echo  Manual recovery options:
            echo    1. Delete the venv folder ^(rd /S /Q "%VENV_DIR%"^) and
            echo       run %~nx0 from a clean state.
            echo    2. Force-reinstall the face stack manually ^(mirrors
            echo       REPAIR_PACKAGES in dependency_health_check.py^):
            rem  Inside () blocks cmd parses each line twice; needs ^^^^ to
            rem  emit a literal ^ for the user (see the mirror block above
            rem  for the full Gemini PR #55 round 2 HIGH explanation).
            echo       "%VENV_PYTHON%" -m pip install --force-reinstall ^^^^
            echo         --no-cache-dir numpy==1.26.4 tensorflow==2.16.2 ^^^^
            echo         tensorflow-intel==2.16.2 protobuf==4.25.3 ^^^^
            echo         tf-keras==2.16.0 retina-face==0.0.17 ^^^^
            echo         deepface==0.0.92 ^^^^
            echo         scipy^>=1.11,^<2 absl-py^>=2.3,^<3
            echo    3. Inspect the diagnostic log at:
            echo       %STATE_DIR%\last_health.log
            echo    4. Inspect the launch log at:
            echo       %LOG_FILE%
            echo(
            >>"%LOG_FILE%" echo [%LAUNCH_TS%] health-repair FAIL ^(fresh-install path^); exiting
            call :release_setup_lock
            pause
            exit /b 1
        )
        >>"%LOG_FILE%" echo [%LAUNCH_TS%] health-repair OK ^(fresh-install path^)
        set "HEALTH_OK=1"
    ) else (
        >>"%LOG_FILE%" echo [%LAUNCH_TS%] health-probe OK ^(fresh-install path^)
        set "HEALTH_OK=1"
    )
    echo   [%LAUNCH_TS%] Runtime health: OK
) else (
    rem No health script present (older/partial tree): preserve legacy
    rem behaviour of caching the stamp -- nothing to verify against.
    set "HEALTH_OK=1"
)

rem --- Write stamp so next launch skips the above. Guarded on HEALTH_OK
rem --- so a venv that failed the health probe (e.g. numpy 2.x re-pulled,
rem --- breaking TF) is NOT cached as healthy -- the next launch re-syncs
rem --- + repairs instead of trusting a broken stamp (the v2.10 fresh-
rem --- install Face Crop bug). HEALTH_OK is set only on a clean probe,
rem --- a successful repair, or when no health script exists to verify.
if defined HEALTH_OK (
    del "%STATE_DIR%\deps_*.ok" >nul 2>&1
    >>"%STAMP%" echo %LAUNCH_TS%
    echo   [%LAUNCH_TS%] Stamp written. Next launch will skip dep sync.
) else (
    >>"%LOG_FILE%" echo [%LAUNCH_TS%] stamp NOT written ^(health not OK^); next launch re-syncs
    echo   [%LAUNCH_TS%] Health not confirmed -- stamp NOT written; next launch will re-sync.
)
echo(

rem --- PR #49: release bootstrap mutex BEFORE launching the GUI -------
call :release_setup_lock

:launch
rem --- Auto-detect NVIDIA + bootstrap CuPy. Runs on BOTH the cached and
rem --- full-sync paths (each falls through / jumps here). Idempotent +
rem --- cached via .launcher_state\gpu_status.json. Never blocks launch
rem --- on failure (script always exits 0). Opt-out:
rem ---     set KLING_SKIP_GPU_BOOTSTRAP=1
if exist "%ROOT_DIR%\scripts\gpu_bootstrap.py" (
    "%VENV_PYTHON%" "%ROOT_DIR%\scripts\gpu_bootstrap.py" --quiet-if-cached
)

echo   [%LAUNCH_TS%] Launching GUI...
echo   Venv: %VENV_PYTHON%
echo(

set "KLING_GUI_CLI_ERRORS=1"
rem  Tee the GUI RUNTIME to the transcript as you use the app (rPPG/crash/etc.).
rem  The dep install already ran on a real console so its progress bars stayed
rem  pretty; only this app-runtime portion is captured. Falls back to a direct
rem  launch if a transcript wasn't set up, the helper is missing, PowerShell is
rem  absent, or the tee infra fails (rc 3). FLAT if/goto (cmd 25H2 paren crash).
if "%TRANSCRIPT_FILE%"=="" goto :gui_launch_direct
if not exist "%ROOT_DIR%\scripts\win_tee_launch.ps1" goto :gui_launch_direct
where powershell >nul 2>&1
if errorlevel 1 goto :gui_launch_direct
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT_DIR%\scripts\win_tee_launch.ps1" "%VENV_PYTHON%" "%TRANSCRIPT_FILE%" -u "%GUI_SCRIPT%" %*
set "EXIT_CODE=!errorlevel!"
if "!EXIT_CODE!"=="3" goto :gui_launch_direct
echo(
echo   App log for this session saved to: %TRANSCRIPT_FILE%
goto :gui_launch_done
:gui_launch_direct
"%VENV_PYTHON%" -u "%GUI_SCRIPT%" %*
set "EXIT_CODE=!errorlevel!"
:gui_launch_done

echo(
if !EXIT_CODE! neq 0 (
    echo   [%LAUNCH_TS%] CRASH -- exit code: !EXIT_CODE!
    echo   Check crash_log.txt for details.
    echo(
)

echo  Press any key to close...
pause >nul
endlocal
exit /b %EXIT_CODE%

:BASE_DEP_FAIL
echo(
echo  ERROR: Base dependency install failed.
echo  This is usually a network problem (a wheel download was interrupted)
echo  or a disk-space / antivirus lock. Check your internet connection,
echo  close any running Python/GUI processes, and re-run %~nx0.
echo  If it still fails, delete the venv folder and run %~nx0 from scratch.
echo(
call :release_setup_lock
pause
endlocal & exit /b 1

:DEPENDENCY_FAIL
echo(
echo  ERROR: Oldcam dependency install failed.
echo  MediaPipe is required for Oldcam v7-v10.
echo  Close running Python/GUI processes and retry.
echo  If it still fails, recreate the venv or run dep repair/bootstrap manually.
echo(
call :release_setup_lock
pause
endlocal & exit /b 1

:release_setup_lock
rem PR #49 round-2 H-1: centralize lock release. Called before every
rem exit/goto out of the bootstrap region so a dep-failure path never
rem leaves the lock dir for the next sibling launcher to wait on.
rem
rem PR #51 round-3 (H1, Windows-side verification): retry once after a
rem 2s sleep when the first rd fails. Common cause is a transient handle
rem held by Windows Defender / Search Indexer / Explorer scan on the
rem lock dir contents. If even the retry fails, log the path to
rem %LOG_FILE% so the user has a breadcrumb to clear it manually
rem (rd /S /Q .launcher_state\setup.lock). Without this, a transient
rem AV hold would leave the lock for the full 24h forfiles-stale
rem window, blocking every sibling launch in between.
rd /S /Q "%SETUP_LOCK%" >nul 2>&1
if not exist "%SETUP_LOCK%" exit /b 0
rem Attempt 2: 2s wait. Covers a quick AV scan releasing its handle.
ping -n 3 127.0.0.1 >nul 2>&1
rd /S /Q "%SETUP_LOCK%" >nul 2>&1
if not exist "%SETUP_LOCK%" exit /b 0
rem Attempt 3: 4s wait. Defender deep-scan on a freshly-extracted wheel
rem can hold handles 5-30s. ~6s total budget covers the common cases.
ping -n 5 127.0.0.1 >nul 2>&1
rd /S /Q "%SETUP_LOCK%" >nul 2>&1
if not exist "%SETUP_LOCK%" exit /b 0
>>"%LOG_FILE%" echo [%LAUNCH_TS%] WARN: setup.lock release failed after 3 attempts; manual cleanup: rd /S /Q "%SETUP_LOCK%"
echo   [setup-lock] WARN: failed to release %SETUP_LOCK% after 3 attempts
echo   [setup-lock] Manual cleanup: rd /S /Q "%SETUP_LOCK%"
exit /b 0

:INSTALL_REQUIREMENTS
set "REQ_FILE=%~1"
set "REQ_KIND=%~2"
set "REQ_FILTERED=%TEMP%\selfiegen_req_%RANDOM%_%RANDOM%.txt"
if not exist "%REQ_FILE%" exit /b 0
findstr /V /I /B "mediapipe" "%REQ_FILE%" > "%REQ_FILTERED%"
rem Guard the constraints flag on file existence (GPT review, PR #65):
rem if constraints.txt is somehow absent, degrade to an unconstrained
rem install instead of pip erroring on a missing -c file. Single inner
rem quotes (NOT doubled) so a path with spaces stays one argument.
set "CC="
if exist "%CONSTRAINTS_FILE%" set "CC=-c "%CONSTRAINTS_FILE%""
echo   Syncing %REQ_KIND% deps from %~nx1...
"%VENV_PYTHON%" -m pip install --only-binary :all: !CC! -r "%REQ_FILTERED%"
if !errorlevel! neq 0 (
    echo   Retrying without binary constraint...
    "%VENV_PYTHON%" -m pip install !CC! -r "%REQ_FILTERED%"
    if !errorlevel! neq 0 (
        del "%REQ_FILTERED%" >nul 2>&1
        exit /b 1
    )
)
findstr /I /R "^[ ]*mediapipe" "%REQ_FILE%" >nul
if !errorlevel! equ 0 (
    echo   Installing MediaPipe separately with --no-deps...
    "%VENV_PYTHON%" -m pip install --no-deps !CC! "%MEDIAPIPE_SPEC%"
    if !errorlevel! neq 0 (
        del "%REQ_FILTERED%" >nul 2>&1
        exit /b 1
    )
    rem v2.17 CRITICAL: mediapipe was just installed --no-deps, so its
    rem RUNTIME deps are NOT present. mediapipe.tasks.python.vision (the
    rem FaceLandmarker rPPG + oldcam use) imports matplotlib at load time +
    rem uses opencv-contrib-python / sounddevice. A bare "import mediapipe"
    rem passes, so the old gate thought it was fine -- then the real import
    rem crashed with "No module named matplotlib" and rPPG fell back to
    rem -NORPPG on EVERY run. setup_macos.sh always installed these three;
    rem the Windows launcher never did (the recurring rPPG bug). numpy<2
    rem pinned so matplotlib->contourpy->numpy cannot upgrade numpy + break TF.
    echo   Installing MediaPipe runtime deps ^(matplotlib/opencv-contrib/sounddevice^)...
    "%VENV_PYTHON%" -m pip install !CC! matplotlib "opencv-contrib-python<4.12" sounddevice "numpy>=1.26,<2"
    if !errorlevel! neq 0 (
        del "%REQ_FILTERED%" >nul 2>&1
        exit /b 1
    )
)
del "%REQ_FILTERED%" >nul 2>&1
exit /b 0
