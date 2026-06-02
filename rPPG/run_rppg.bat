@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

rem rPPG injector launcher -- runs off the repo main venv.
rem This dir is gitignored (sensitive friend tool); never committed.
rem All deps already live in the shared repo venv -- no pip step.

rem Codex P1 / P2 (2026-05-21): when invoked from a Python subprocess
rem (queue_manager._rppg_video, automation/rppg.py:run_rppg) the four
rem error-path pause + end-of-file pause statements blocked indefinitely
rem waiting for keypress on a hidden stdin. Now suppressed via the
rem KLING_NO_PAUSE env var which the Python callers always set; manual
rem double-click users still get the pauses (var unset). The PAUSE alias
rem call expands to "pause" or nothing per the gate.
set "PAUSE=pause"
if defined KLING_NO_PAUSE set "PAUSE=rem skip_pause"

set "REPO_ROOT="
if exist "..\requirements.txt" if exist "..\kling_automation_ui.py" for %%I in ("..") do set "REPO_ROOT=%%~fI"
if not defined REPO_ROOT (
  echo  [ERROR] Could not locate repo root from %CD%.
  %PAUSE%
  exit /b 1
)
set "STATE_DIR=%REPO_ROOT%\.launcher_state"
if not exist "%STATE_DIR%" mkdir "%STATE_DIR%" >nul 2>&1
set "LOG_FILE=%STATE_DIR%\rppg.log"

set "PYTHON_BIN="
set "ENV_KIND="
rem Codex P1 (2026-05-22): if SELFIEGEN_PYTHON is set but invalid,
rem reject loudly instead of silently falling back. Silent fallback
rem hides the user's override mistake.
if not "%SELFIEGEN_PYTHON%"=="" (
  "%SELFIEGEN_PYTHON%" -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] < (3,13) else 2)" >nul 2>&1
  if errorlevel 1 (
    echo   ERROR: SELFIEGEN_PYTHON is set to "%SELFIEGEN_PYTHON%" but
    echo   that interpreter is outside the supported range ^(3.9-3.12^),
    echo   doesn't exist, or isn't executable. Either fix the env var or
    echo   unset it to fall back to the venv resolver chain.
    >>"%LOG_FILE%" echo [ERROR] SELFIEGEN_PYTHON rejected: %SELFIEGEN_PYTHON%
    %PAUSE%
    exit /b 1
  )
  set "PYTHON_BIN=%SELFIEGEN_PYTHON%"
  set "ENV_KIND=SELFIEGEN_PYTHON override"
)
if "!PYTHON_BIN!"=="" call :check_py "%REPO_ROOT%\venv\Scripts\python.exe" "shared root venv"
if "!PYTHON_BIN!"=="" call :check_py "%REPO_ROOT%\.venv311\Scripts\python.exe" "shared root .venv311"
if "!PYTHON_BIN!"=="" call :check_py "%REPO_ROOT%\.venv\Scripts\python.exe" "shared root .venv"
if "!PYTHON_BIN!"=="" (
  echo   ERROR: No supported Python ^(3.9-3.12^) found in the repo venv.
  echo   Create it: py -3.11 -m venv "%REPO_ROOT%\venv" then pip install -r requirements.txt
  >>"%LOG_FILE%" echo [ERROR] No supported Python found.
  %PAUSE%
  exit /b 1
)
echo   Python: !ENV_KIND! -- !PYTHON_BIN!
>>"%LOG_FILE%" echo [INFO] Using !ENV_KIND!: !PYTHON_BIN!

"!PYTHON_BIN!" -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] < (3,13) else 2)" >nul 2>&1
if errorlevel 1 (
  echo   ERROR: Resolved Python outside supported range 3.9-3.12.
  >>"%LOG_FILE%" echo [ERROR] Unsupported Python after resolve.
  %PAUSE%
  exit /b 1
)
"!PYTHON_BIN!" -c "import cv2, numpy, mediapipe, scipy" >nul 2>&1
if errorlevel 1 (
  rem v2.7 friend-zip self-heal (PR #54 / 2026-05-27): the prior block
  rem only ECHOED the sync command and exited. A user opening a fresh
  rem personal zip whose venv was built before scipy/mediapipe joined
  rem requirements.txt then hit the dead-end '... missing cv2/numpy/
  rem mediapipe/scipy' error and rPPG silently failed every run. Now we
  rem ACTUALLY run the pip install against the resolved !PYTHON_BIN!
  rem (NOT a hardcoded %REPO_ROOT%\venv\Scripts\pip, which can resolve
  rem to a different python on .venv311 / SELFIEGEN_PYTHON hosts) and
  rem re-run the import check. Honours KLING_NO_PAUSE so the GUI
  rem subprocess doesn't wedge on a pause.
  echo   WARN: rPPG deps missing -- syncing repo requirements before retry...
  >>"%LOG_FILE%" echo [WARN] Core imports missing; running pip install.
  rem Concurrent rPPG launches (two GUI windows) must not both run pip
  rem against the shared venv. mkdir-based atomic lock; sibling waits up
  rem to ~10 min then proceeds (matches the launcher's setup.lock TTL).
  set "RPPG_SETUP_LOCK=%STATE_DIR%\rppg_setup.lock"
  set "RPPG_LOCK_WAITED="
  set "RPPG_LOCK_TRIES=0"
  :rppg_setup_lock_acquire
  md "!RPPG_SETUP_LOCK!" >nul 2>&1
  if !errorlevel! equ 0 goto :rppg_setup_lock_acquired
  forfiles /P "%STATE_DIR%" /M rppg_setup.lock /D -1 >nul 2>&1
  if !errorlevel! equ 0 (
    echo   [rppg-setup-lock] removing stale lock
    rmdir /S /Q "!RPPG_SETUP_LOCK!" >nul 2>&1
    rem NO goto here (gemini HIGH PR #54): if rmdir fails (perms/open
    rem handle) a goto back to :acquire would spin at 100%% CPU forever,
    rem bypassing the RPPG_LOCK_TRIES counter + ping sleep below. Fall
    rem through so every spin increments the counter and eventually
    rem hits the geq-930 graceful give-up.
  )
  rem forfiles /D -1 only matches locks >=1 DAY old, so a lock left by a
  rem sibling that crashed earlier the SAME day would hang here forever.
  rem Bound the wait with a retry counter, but keep the window LARGER than a
  rem worst-case full-requirements install so we never force-break a LIVE
  rem sibling lock mid-pip (codex P2 PR #54 -- mirrors gpu_bootstrap's
  rem LOCK_STALE_SECONDS > PIP_INSTALL_TIMEOUT rule). The root requirements
  rem (TensorFlow etc.) can take ~20-30 min on a thin line, so ~900 iters *
  rem ~2s/ping ~= 30 min before force-break; hard-give-up ~1 min later if the
  rem lock dir genuinely cannot be removed.
  set /a RPPG_LOCK_TRIES+=1
  if !RPPG_LOCK_TRIES! geq 930 (
    echo   ERROR: rPPG setup lock stuck and could not be cleared.
    >>"%LOG_FILE%" echo [ERROR] rppg_setup.lock stuck; giving up.
    %PAUSE%
    exit /b 1
  )
  if !RPPG_LOCK_TRIES! geq 900 (
    echo   [rppg-setup-lock] lock held too long; force-breaking and proceeding
    >>"%LOG_FILE%" echo [WARN] rppg_setup.lock force-broken after timeout.
    rmdir /S /Q "!RPPG_SETUP_LOCK!" >nul 2>&1
    goto :rppg_setup_lock_acquire
  )
  if not defined RPPG_LOCK_WAITED (
    echo   [rppg-setup-lock] another launcher is syncing rPPG deps; waiting...
    set "RPPG_LOCK_WAITED=1"
  )
  ping -n 3 127.0.0.1 >nul 2>&1
  goto :rppg_setup_lock_acquire
  :rppg_setup_lock_acquired
  rem Re-check imports after acquiring the lock - a sibling may have
  rem ALREADY installed them while we waited, in which case we can skip
  rem the pip work entirely.
  "!PYTHON_BIN!" -c "import cv2, numpy, mediapipe, scipy" >nul 2>&1
  if !errorlevel! equ 0 (
    rmdir /S /Q "!RPPG_SETUP_LOCK!" >nul 2>&1
    echo   OK: rPPG deps installed by sibling launcher; continuing.
    goto :rppg_post_dep_check
  )
  call :rppg_sync_deps
  rmdir /S /Q "!RPPG_SETUP_LOCK!" >nul 2>&1
  if !PIP_EXIT! neq 0 (
    echo   ERROR: pip install -r requirements.txt failed.
    >>"%LOG_FILE%" echo [ERROR] pip install -r requirements.txt failed.
    %PAUSE%
    exit /b 1
  )
  :rppg_post_dep_check
  rem Re-check imports after the self-heal install with the granular
  rem per-module diagnostic (v2.16). The old check swallowed stderr and
  rem printed its find_spec hint to the console only, so the rppg.log a
  rem user reads showed a useless "Core imports missing" with NO module
  rem name. rppg_import_diag.py reports each module OK/MISSING/BROKEN plus
  rem the actual ImportError and the installed numpy version, and
  rem :rppg_diag_tee mirrors EVERY line to BOTH the console (-> GUI stream)
  rem AND rppg.log so the failing module is always visible.
  call :rppg_diag_tee "post-self-heal import check"
  if !RPPG_DIAG_EXIT! neq 0 (
    echo   ERROR: rPPG deps still missing after pip sync ^(see detail above^).
    >>"%LOG_FILE%" echo [ERROR] Self-heal pip install did not satisfy imports ^(detail above^).
    %PAUSE%
    exit /b 1
  )
  echo   OK: rPPG deps installed.
  >>"%LOG_FILE%" echo [INFO] Self-heal pip install succeeded; continuing.
)
if exist "%REPO_ROOT%\face_landmarker.task" set "MEDIAPIPE_FACE_LANDMARKER_MODEL=%REPO_ROOT%\face_landmarker.task"
rem rppg_injector visualize_analysis() calls plt.show() which BLOCKS on a
rem GUI window; force headless Agg so it never waits for a window close.
set "MPLBACKEND=Agg"
rem v2.7 fix: flush every `print` from the injector child immediately so the
rem GUI sees natural progress cadence instead of a multi-minute silent gap
rem while MediaPipe loads + baseline ROIs extract. The wrapper streamer ALSO
rem sets this in its subprocess env (belt + suspenders).
set "PYTHONUNBUFFERED=1"

echo   Launching rppg_injector.py %*
>>"%LOG_FILE%" echo [INFO] Launching rppg_injector.py %*
"!PYTHON_BIN!" rppg_injector.py %*
set "EXIT_CODE=%ERRORLEVEL%"
echo   Finished with code %EXIT_CODE%.
>>"%LOG_FILE%" echo [INFO] Finished with code %EXIT_CODE%.
%PAUSE%
exit /b %EXIT_CODE%

:check_py
if "%~1"=="" exit /b 1
if not exist "%~1" exit /b 1
"%~1" -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] < (3,13) else 2)" >nul 2>&1
if errorlevel 1 exit /b 1
set "PYTHON_BIN=%~1"
set "ENV_KIND=%~2"
exit /b 0

:rppg_diag_tee
rem Run the granular per-module import diagnostic and mirror its output to
rem BOTH the console (so the GUI subprocess stream captures it) AND rppg.log
rem (so a user reading the log file sees the same detail). %~1 is a short
rem context label for the log. Sets RPPG_DIAG_EXIT to the helper exit code
rem (0 = all modules import, 1 = at least one MISSING/BROKEN).
set "RPPG_DIAG_EXIT=1"
set "RPPG_DIAG_TMP=%TEMP%\rppg_diag_%RANDOM%_%RANDOM%.txt"
>>"%LOG_FILE%" echo [INFO] rPPG import diagnostic (%~1):
"!PYTHON_BIN!" "%REPO_ROOT%\scripts\rppg_import_diag.py" > "!RPPG_DIAG_TMP!" 2>&1
set "RPPG_DIAG_EXIT=!errorlevel!"
if exist "!RPPG_DIAG_TMP!" (
  type "!RPPG_DIAG_TMP!"
  type "!RPPG_DIAG_TMP!" >>"%LOG_FILE%"
  del "!RPPG_DIAG_TMP!" >nul 2>&1
)
exit /b !RPPG_DIAG_EXIT!

:rppg_sync_deps
rem P1 (codex PR #54): MediaPipe must install with --no-deps (Hard Rule #6).
rem Installing the full requirements.txt with normal dependency resolution
rem lets pip pull MediaPipe's own deps and break the TF/protobuf/numpy stack.
rem Mirror launchers\windows\run_gui.bat :INSTALL_REQUIREMENTS -- filter
rem mediapipe out, install the rest (prefer wheels, fall back to source),
rem then install the EXACT pinned mediapipe line read FROM requirements.txt
rem with --no-deps. Reading the pin dynamically (not a hardcoded literal)
rem keeps this self-heal from drifting when the requirements pin is bumped.
set "RPPG_REQ_FILTERED=%TEMP%\rppg_req_%RANDOM%_%RANDOM%.txt"
findstr /V /I /B "mediapipe" "%REPO_ROOT%\requirements.txt" > "%RPPG_REQ_FILTERED%"
"!PYTHON_BIN!" -m pip install --only-binary :all: -c "%REPO_ROOT%\constraints.txt" -r "%RPPG_REQ_FILTERED%"
set "PIP_EXIT=!errorlevel!"
if !PIP_EXIT! neq 0 (
  echo   Retrying without binary constraint...
  "!PYTHON_BIN!" -m pip install -c "%REPO_ROOT%\constraints.txt" -r "%RPPG_REQ_FILTERED%"
  set "PIP_EXIT=!errorlevel!"
)
if !PIP_EXIT! neq 0 (
  del "%RPPG_REQ_FILTERED%" >nul 2>&1
  exit /b !PIP_EXIT!
)
rem v2.13: extract the mediapipe spec via a real parser that skips COMMENT
rem lines. The old `findstr /R "^[ ]*mediapipe"` had its anchor carets
rem mangled inside this for /f backtick context, so it matched the FIRST
rem "mediapipe" line in requirements.txt -- a COMMENT -- and pip choked on
rem the embedded ";" (InvalidMarker), failing rPPG. The helper returns the
rem real `mediapipe==` line (or the fallback) and nothing else. The inner
rem command is wrapped in `cmd /c "..."` because a bare caret-quoted first
rem token (^"...^") makes for /f's tokenizer error out + capture nothing
rem (code-review HIGH, PR #65) -- the cmd /c wrapper captures correctly,
rem verified live with spaced + non-spaced REPO_ROOT.
set "RPPG_MEDIAPIPE_SPEC="
for /f "usebackq delims=" %%M in (`cmd /c ^"^"!PYTHON_BIN!^" ^"%REPO_ROOT%\scripts\read_requirement_spec.py^" mediapipe ^"%REPO_ROOT%\requirements.txt^" mediapipe==0.10.35^"`) do (
  if not defined RPPG_MEDIAPIPE_SPEC set "RPPG_MEDIAPIPE_SPEC=%%M"
)
if not defined RPPG_MEDIAPIPE_SPEC set "RPPG_MEDIAPIPE_SPEC=mediapipe==0.10.35"
if defined RPPG_MEDIAPIPE_SPEC (
  echo   Installing MediaPipe separately with --no-deps: !RPPG_MEDIAPIPE_SPEC!
  "!PYTHON_BIN!" -m pip install --no-deps -c "%REPO_ROOT%\constraints.txt" "!RPPG_MEDIAPIPE_SPEC!"
  set "PIP_EXIT=!errorlevel!"
)
del "%RPPG_REQ_FILTERED%" >nul 2>&1
exit /b !PIP_EXIT!