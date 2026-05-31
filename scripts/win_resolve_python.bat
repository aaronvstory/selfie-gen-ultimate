@echo off
rem ============================================================
rem  scripts\win_resolve_python.bat  --  shared Windows Python resolver
rem ------------------------------------------------------------
rem  CALL this from a launcher AFTER setlocal enabledelayedexpansion.
rem  It runs in the caller's environment (no setlocal of its own) so the
rem  variables it sets survive the return:
rem    VENV_PYTHON      -> full path to the venv python.exe (on success)
rem    RESOLVED_PYTHON  -> the interpreter used to create/locate the venv
rem    RESOLVE_RC       -> 0 on success, 1 on failure
rem
rem  Caller contract (set BEFORE calling):
rem    ROOT_DIR    -> repo root (the dir that holds venv\). REPO_ROOT is
rem                   accepted too and aliased from ROOT_DIR if unset.
rem    VENV_DIR    -> %ROOT_DIR%\venv
rem    VENV_PYTHON -> %VENV_DIR%\Scripts\python.exe (re-set here on success)
rem    STATE_DIR   -> %ROOT_DIR%\.launcher_state (for the log)
rem    LOG_FILE    -> launch log path
rem    LAUNCH_TS   -> timestamp string for log lines
rem
rem  Resolution order (each candidate version-gated to 3.9-3.12 via the
rem  flat-goto :pyres_check, mirroring oldcam-v24/oldcam_launcher.bat):
rem    1. existing venv (%VENV_PYTHON%, .venv311, .venv)
rem    2. SELFIEGEN_PYTHON / SELFIEGEN_VENV_DIR overrides (STRICT-gated)
rem    3. py launcher: py -3.11, -3.12, -3.10, -3.9 (works WITHOUT PATH
rem       -- the fix for the 'installed but not on PATH' case)
rem    4. python on PATH (last; may be 3.13+)
rem    5. common install dirs (LocalAppData / Program Files / C:\PythonXY)
rem    6. auto-install Python 3.12 (winget -> python.org silent installer),
rem       then re-probe via the py launcher / absolute path
rem
rem  mediapipe==0.10.35 has wheels for 3.9-3.12 ONLY, so auto-install MUST
rem  target 3.12 (not 'latest') or the next run fails the version gate.
rem
rem  cmd-parser safety: every version probe + every paren-bearing echo lives
rem  in a FLAT subroutine (no if/else paren-blocks), so the (3,9)/(3,13)
rem  literals can never close an enclosing block early. echo( (no space) is
rem  the safe blank-line idiom. See tests/test_win_python_resolver.py.
rem ============================================================

set "RESOLVE_RC=1"
set "RESOLVED_PYTHON="
set "PYRES_BIN="
set "PYRES_KIND="

rem  Callers set ROOT_DIR (not REPO_ROOT). Alias it so the existing-venv
rem  fallback probes below actually run. Honour a pre-set REPO_ROOT too.
if not defined REPO_ROOT if defined ROOT_DIR set "REPO_ROOT=%ROOT_DIR%"

rem --- 1/2. Existing venvs + env overrides (version-gated) ---------------
rem  Use `if defined` for env vars (an unquoted %VAR% with parens/&/| inside
rem  an if-condition can crash the parser; `if defined` is expansion-safe).
if exist "%VENV_PYTHON%" call :pyres_check "%VENV_PYTHON%" "existing venv" strict
if "!PYRES_BIN!"=="" if defined SELFIEGEN_PYTHON call :pyres_try_override
if "!PYRES_BIN!"=="" if defined SELFIEGEN_VENV_DIR call :pyres_check "%SELFIEGEN_VENV_DIR%\Scripts\python.exe" "SELFIEGEN_VENV_DIR override" strict
if "!PYRES_BIN!"=="" if defined REPO_ROOT call :pyres_check "%REPO_ROOT%\venv\Scripts\python.exe" "shared root venv" strict
if "!PYRES_BIN!"=="" if defined REPO_ROOT call :pyres_check "%REPO_ROOT%\.venv311\Scripts\python.exe" "shared root .venv311" strict
if "!PYRES_BIN!"=="" if defined REPO_ROOT call :pyres_check "%REPO_ROOT%\.venv\Scripts\python.exe" "shared root .venv" strict

rem If an existing venv (or an override venv python) resolved, we're done.
if not "!PYRES_BIN!"=="" call :pyres_use_existing
if "!RESOLVE_RC!"=="0" goto :pyres_done

rem --- 3. py launcher (works without 'Add to PATH') ---------------------
rem  The py.exe launcher ships with every python.org installer and selects
rem  by version from the registry, so it finds an interpreter the user
rem  installed but never added to PATH -- the exact failure that motivated
rem  this resolver. Prefer 3.11 then 3.12 (both have mediapipe wheels).
call :pyres_try_py 3.11
if "!RESOLVED_PYTHON!"=="" call :pyres_try_py 3.12
if "!RESOLVED_PYTHON!"=="" call :pyres_try_py 3.10
if "!RESOLVED_PYTHON!"=="" call :pyres_try_py 3.9

rem --- 4. python on PATH (last; could be an unsupported 3.13+) ----------
if "!RESOLVED_PYTHON!"=="" call :pyres_try_path_python

rem --- 5. Common install dirs (glob both 3.11 and 3.12) -----------------
if "!RESOLVED_PYTHON!"=="" call :pyres_try_dir "%LocalAppData%\Programs\Python\Python311\python.exe"
if "!RESOLVED_PYTHON!"=="" call :pyres_try_dir "%LocalAppData%\Programs\Python\Python312\python.exe"
if "!RESOLVED_PYTHON!"=="" call :pyres_try_dir "%ProgramFiles%\Python311\python.exe"
if "!RESOLVED_PYTHON!"=="" call :pyres_try_dir "%ProgramFiles%\Python312\python.exe"
if "!RESOLVED_PYTHON!"=="" call :pyres_try_dir "C:\Python311\python.exe"
if "!RESOLVED_PYTHON!"=="" call :pyres_try_dir "C:\Python312\python.exe"

rem --- 6. Auto-install Python 3.12 if STILL nothing ---------------------
if "!RESOLVED_PYTHON!"=="" call :pyres_install_python

rem --- No interpreter at all: actionable failure + open python.org ------
if "!RESOLVED_PYTHON!"=="" goto :pyres_no_python

rem --- Create the venv from the resolved interpreter --------------------
call :pyres_create_venv
goto :pyres_done

:pyres_no_python
echo(
echo  ============================================================
echo  ERROR: No supported Python ^(3.9-3.12^) found and auto-install
echo  did not complete ^(it may be blocked by antivirus or policy^).
echo  ============================================================
echo  Install Python 3.12 from https://www.python.org/downloads/
echo  then just double-click this launcher again -- no PATH setup needed.
echo  ============================================================
echo(
>>"%LOG_FILE%" echo [%LAUNCH_TS%] resolver: no python and auto-install failed
start "" "https://www.python.org/downloads/" >nul 2>&1
set "RESOLVE_RC=1"
goto :pyres_done

:pyres_done
goto :eof

rem ============================================================
rem :pyres_use_existing  -- adopt the venv/override python found in step 1/2
rem ============================================================
:pyres_use_existing
echo   [%LAUNCH_TS%] Python: !PYRES_KIND! -- !PYRES_BIN!
>>"%LOG_FILE%" echo [%LAUNCH_TS%] resolver: !PYRES_KIND! -- !PYRES_BIN!
set "VENV_PYTHON=!PYRES_BIN!"
set "RESOLVED_PYTHON=!PYRES_BIN!"
set "RESOLVE_RC=0"
goto :eof

rem ============================================================
rem :pyres_try_override  -- accept SELFIEGEN_PYTHON only if it runs AND is
rem   in the supported 3.9-3.12 range (strict gate -- an unsupported override
rem   like 3.13 must fall through to the other candidates, not be adopted).
rem   Strips surrounding quotes the user may have wrapped the value in.
rem ============================================================
:pyres_try_override
set "PYRES_OV=%SELFIEGEN_PYTHON:"=%"
if "!PYRES_OV!"=="" goto :eof
"!PYRES_OV!" -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] < (3,13) else 2)" >nul 2>&1
if errorlevel 1 goto :eof
set "PYRES_BIN=!PYRES_OV!"
set "PYRES_KIND=SELFIEGEN_PYTHON override"
goto :eof

rem ============================================================
rem :pyres_try_path_python  -- accept `python` on PATH only if supported
rem ============================================================
:pyres_try_path_python
where python >nul 2>&1
if errorlevel 1 goto :eof
python -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] < (3,13) else 2)" >nul 2>&1
if errorlevel 1 goto :eof
set "RESOLVED_PYTHON=python"
echo   [%LAUNCH_TS%] Found supported python on PATH.
>>"%LOG_FILE%" echo [%LAUNCH_TS%] resolver: python on PATH selected
goto :eof

rem ============================================================
rem :pyres_try_py <X.Y>  -- if `py -X.Y` exists and is supported, record it
rem   Sets RESOLVED_PYTHON to the literal `py -X.Y` command string.
rem ============================================================
:pyres_try_py
py -%~1 -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] < (3,13) else 2)" >nul 2>&1
if errorlevel 1 goto :eof
set "RESOLVED_PYTHON=py -%~1"
echo   [%LAUNCH_TS%] Found Python via py launcher: py -%~1
>>"%LOG_FILE%" echo [%LAUNCH_TS%] resolver: py -%~1 selected
goto :eof

rem ============================================================
rem :pyres_try_dir <full-path-to-python.exe>  -- common install dir probe
rem   Stores a BARE path (no embedded quotes); the create step quotes it.
rem ============================================================
:pyres_try_dir
if not exist "%~1" goto :eof
"%~1" -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] < (3,13) else 2)" >nul 2>&1
if errorlevel 1 goto :eof
set "RESOLVED_PYTHON=%~1"
echo   [%LAUNCH_TS%] Found Python in: %~1
>>"%LOG_FILE%" echo [%LAUNCH_TS%] resolver: install-dir python %~1
goto :eof

rem ============================================================
rem :pyres_create_venv  -- create venv from !RESOLVED_PYTHON!, then gate it
rem ============================================================
:pyres_create_venv
echo   [%LAUNCH_TS%] Creating virtual environment with: !RESOLVED_PYTHON!
>>"%LOG_FILE%" echo [%LAUNCH_TS%] resolver: creating venv via !RESOLVED_PYTHON!
rem  RESOLVED_PYTHON is either a bare command ("py -3.12" / "python") or a
rem  full python.exe path. Quote the path form (may contain spaces); leave
rem  the multi-token "py -3.x" form unquoted (quoting it would break it).
rem  Detect a path by testing for a backslash via batch substring removal
rem  (findstr /C:"\" errors with 'escape sequence expected' -- do NOT use it).
if not "!RESOLVED_PYTHON!"=="!RESOLVED_PYTHON:\=!" (
    "!RESOLVED_PYTHON!" -m venv "%VENV_DIR%"
) else (
    !RESOLVED_PYTHON! -m venv "%VENV_DIR%"
)
if errorlevel 1 goto :pyres_create_fail
if not "%STATE_DIR%"=="" del "%STATE_DIR%\deps_*.ok" >nul 2>&1
call :pyres_check "%VENV_DIR%\Scripts\python.exe" "created venv" strict
if "!PYRES_BIN!"=="" goto :pyres_create_badver
set "VENV_PYTHON=!PYRES_BIN!"
set "RESOLVED_PYTHON=!PYRES_BIN!"
set "RESOLVE_RC=0"
echo   [%LAUNCH_TS%] Virtual environment ready.
>>"%LOG_FILE%" echo [%LAUNCH_TS%] resolver: venv ready -- !VENV_PYTHON!
goto :eof
:pyres_create_fail
echo(
echo  ERROR: Failed to create the virtual environment.
echo  The resolved Python could not create a venv. Close any running
echo  Python/GUI processes and try again.
echo(
>>"%LOG_FILE%" echo [%LAUNCH_TS%] resolver: venv creation FAILED via !RESOLVED_PYTHON!
set "RESOLVE_RC=1"
goto :eof
:pyres_create_badver
echo  ERROR: Created a venv but its Python failed the 3.9-3.12 gate.
>>"%LOG_FILE%" echo [%LAUNCH_TS%] resolver: created venv failed version gate
set "RESOLVE_RC=1"
goto :eof

rem ============================================================
rem :pyres_install_python  -- silent auto-install of Python 3.12
rem   Tries winget first, then a python.org silent installer download.
rem   The installers are run with `start /wait` so the batch BLOCKS until
rem   they finish (a bare invocation returns immediately and the re-probe
rem   would run before any files exist; the del would also hit a locked
rem   file). PATH edits from the installer do NOT reach this running shell,
rem   so the re-probe goes through the py launcher / absolute paths only.
rem ============================================================
:pyres_install_python
echo(
echo  ============================================================
echo   No Python found -- installing Python 3.12 automatically.
echo   This is a one-time step and takes ~1-3 minutes. Please wait...
echo  ============================================================
echo(
>>"%LOG_FILE%" echo [%LAUNCH_TS%] resolver: auto-install starting
where winget >nul 2>&1
if errorlevel 1 goto :pyres_install_pyorg
echo   [%LAUNCH_TS%] Installing via winget ^(Python.Python.3.12^)...
winget install -e --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements --disable-interactivity --override "/quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1"
echo   [%LAUNCH_TS%] winget step finished; re-detecting...
call :pyres_try_py 3.12
if not "!RESOLVED_PYTHON!"=="" goto :pyres_install_ok
call :pyres_try_py 3.11
if not "!RESOLVED_PYTHON!"=="" goto :pyres_install_ok
call :pyres_try_dir "%LocalAppData%\Programs\Python\Python312\python.exe"
if not "!RESOLVED_PYTHON!"=="" goto :pyres_install_ok

:pyres_install_pyorg
echo   [%LAUNCH_TS%] Downloading the official Python 3.12 installer from python.org...
>>"%LOG_FILE%" echo [%LAUNCH_TS%] resolver: trying python.org silent installer
set "PYRES_DL=%TEMP%\python-3.12.10-amd64.exe"
set "PYRES_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; try { [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -UseBasicParsing -Uri $env:PYRES_URL -OutFile $env:PYRES_DL } catch { exit 1 }"
if errorlevel 1 goto :pyres_install_dl_fail
echo   [%LAUNCH_TS%] Running the Python 3.12 installer silently...
start /wait "" "%PYRES_DL%" /quiet InstallAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
call :pyres_try_py 3.12
if "!RESOLVED_PYTHON!"=="" call :pyres_try_dir "%LocalAppData%\Programs\Python\Python312\python.exe"
del "%PYRES_DL%" >nul 2>&1
goto :pyres_install_ok
:pyres_install_dl_fail
echo   [%LAUNCH_TS%] ERROR: could not download the Python installer ^(no network or blocked^).
>>"%LOG_FILE%" echo [%LAUNCH_TS%] resolver: python.org download FAILED
goto :eof
:pyres_install_ok
if "!RESOLVED_PYTHON!"=="" goto :pyres_install_norecheck
echo   [%LAUNCH_TS%] Python 3.12 installed and detected.
>>"%LOG_FILE%" echo [%LAUNCH_TS%] resolver: auto-install OK -- !RESOLVED_PYTHON!
goto :eof
:pyres_install_norecheck
>>"%LOG_FILE%" echo [%LAUNCH_TS%] resolver: auto-install completed but no python re-detected
goto :eof

rem ============================================================
rem :pyres_check "<path>" "<kind>" [permissive^|strict]
rem   Flat goto flow (NO if/else paren-blocks) so the version-probe
rem   string's (3,9)/(3,13) parens cannot prematurely close a block.
rem   On success sets PYRES_BIN + PYRES_KIND.
rem   Invoked via `call`, so `exit /b` here returns from the subroutine
rem   to the caller (it does NOT terminate the resolver) -- this is the
rem   same idiom oldcam-v24/similarity/resemble-score launchers use.
rem ============================================================
:pyres_check
if "%~1"=="" exit /b 1
if not exist "%~1" exit /b 1
if /i "%~3"=="permissive" goto :pyres_check_permissive
"%~1" -c "import sys; raise SystemExit(0 if (3,9) <= sys.version_info[:2] < (3,13) else 2)" >nul 2>&1
if errorlevel 1 exit /b 1
goto :pyres_check_ok
:pyres_check_permissive
"%~1" -V >nul 2>&1
if errorlevel 1 exit /b 1
:pyres_check_ok
set "PYRES_BIN=%~1"
set "PYRES_KIND=%~2"
exit /b 0
