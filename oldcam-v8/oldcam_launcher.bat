@echo off
setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%" >nul

set "PYTHON_CMD="
set "HAD_ERRORS="

if exist ".venv\Scripts\python.exe" (
  .venv\Scripts\python.exe -c "import cv2, numpy" >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=.venv\Scripts\python.exe"
)

if not defined PYTHON_CMD (
  for %%V in (3.12 3.11 3.10 3.9) do (
    py -%%V -c "import cv2, numpy" >nul 2>nul
    if not errorlevel 1 (
      set "PYTHON_CMD=py -%%V"
      goto :python_found
    )
  )
)

if not defined PYTHON_CMD (
  python -c "import cv2, numpy" >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=python"
)

:python_found
if not defined PYTHON_CMD (
  echo Could not find a Python interpreter with both cv2 and numpy installed.
  echo Install the dependencies for your active Python and try again.
  goto DONE
)

rem Optional tuning flags. Edit this line if you want different defaults.
set "EXTRA_ARGS="
if defined OLDCAM_EXTRA_ARGS set "EXTRA_ARGS=%OLDCAM_EXTRA_ARGS%"

if "%~1"=="" goto PICK_FILES
goto PROCESS_ARGS

:PICK_FILES
echo Select one or more media files to process with Oldcam V8.
set "SELECTION_FILE=%TEMP%\oldcam_selection_%RANDOM%%RANDOM%.txt"

powershell -NoProfile -STA -Command ^
  "Add-Type -AssemblyName System.Windows.Forms; " ^
  "$dialog = New-Object System.Windows.Forms.OpenFileDialog; " ^
  "$dialog.Multiselect = $true; " ^
  "$dialog.Filter = 'Media Files|*.mp4;*.mov;*.avi;*.mkv;*.webm;*.m4v;*.jpg;*.jpeg;*.png;*.bmp;*.webp|All Files|*.*'; " ^
  "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { $dialog.FileNames | Set-Content -Path '%SELECTION_FILE%' }"

if not exist "%SELECTION_FILE%" (
  echo No files selected.
  goto DONE
)

for /f "usebackq delims=" %%F in ("%SELECTION_FILE%") do call :PROCESS_ONE "%%F"
del "%SELECTION_FILE%" >nul 2>nul
goto DONE

:PROCESS_ARGS
if "%~1"=="" goto DONE
call :PROCESS_ONE "%~1"
shift
goto PROCESS_ARGS

:PROCESS_ONE
echo(
echo ===============================================================
echo Processing: %~1
call %PYTHON_CMD% "%SCRIPT_DIR%oldcam.py" "%~1" %EXTRA_ARGS%
set "STATUS=%ERRORLEVEL%"
if not "%STATUS%"=="0" (
  echo Failed: %~1
  set "HAD_ERRORS=1"
) else (
  echo Finished: %~1
)
exit /b 0

:DONE
echo(
if defined HAD_ERRORS (
  echo Completed with one or more errors.
) else (
  echo All done.
)
if not defined OLDCAM_NO_PAUSE pause
popd >nul
endlocal
if defined HAD_ERRORS exit /b 1
exit /b 0
