<#
.SYNOPSIS
  Re-launch a Windows .bat through Tee-Object so the WHOLE run is shown live on
  screen AND written to a transcript file.

.DESCRIPTION
  run_gui.bat / run_cli.bat call this helper once (guarded by KLING_TRANSCRIPT)
  to capture every line the launcher + GUI print to the console into a rolling
  transcript under .launcher_state\. The user asked to be able to just copy the
  whole terminal window (or hand over one file) when reporting an issue, instead
  of hunting for scattered logs (matches the macOS `tee` behaviour in the
  .command launchers).

  Using a real .ps1 (instead of an inline `powershell -Command "..."` string in
  the .bat) avoids the cmd<->PowerShell quote-nesting that mangles spaced paths
  and drops exit codes. Args are passed positionally; the child .bat inherits
  KLING_TRANSCRIPT=1 so it skips the wrapper and runs its real body.

  Best-effort by contract: any failure here must NOT block the launch — the
  caller falls back to a direct (un-teed) run. Exit code mirrors the child's.

.PARAMETER BatPath
  Absolute path to the .bat to run (the launcher re-invoking itself).

.PARAMETER TranscriptPath
  Absolute path to the transcript file to write.

.PARAMETER BatArgs
  Remaining args, forwarded verbatim to the .bat.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $BatPath,
    [Parameter(Mandatory = $true)] [string] $TranscriptPath,
    [Parameter(ValueFromRemainingArguments = $true)] [string[]] $BatArgs
)

$ErrorActionPreference = 'Continue'

# Child .bat sees this and skips the tee wrapper (no infinite relaunch).
$env:KLING_TRANSCRIPT = '1'

function Invoke-TranscriptPrune {
    # Keep only the newest 5 transcript-*.log (code-review MEDIUM #5): done HERE
    # in a real .ps1 (where $TranscriptPath is a typed [string] parameter) rather
    # than as an inline `powershell -Command "...'%STATE_DIR%'..."` in the .bat,
    # where a single quote in the install path would break the PowerShell string.
    # Run AFTER the launch so THIS run's transcript (created by Tee-Object during
    # the run) is counted — otherwise steady-state leaves 6 files, not 5.
    # Best-effort: pruning must never block or fail the launch.
    try {
        $tDir = Split-Path -Parent $TranscriptPath
        if ($tDir -and (Test-Path -LiteralPath $tDir)) {
            Get-ChildItem -LiteralPath $tDir -Filter 'transcript-*.log' -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending |
                Select-Object -Skip 5 |
                Remove-Item -Force -ErrorAction SilentlyContinue
        }
    } catch {
        # pruning is never allowed to block the launch
    }
}

# When the target is a .bat/.cmd, run from ITS directory so a relative %~dp0 /
# cmd CWD is always valid (a bad inherited CWD prints "The system cannot find
# the path specified."). When the target is an EXE (e.g. the GUI is launched as
# venv\Scripts\python.exe -u gui_launcher.py), do NOT cd into the venv Scripts
# dir — keep the caller's inherited CWD so the app sees the same working dir it
# would on a direct launch.
$ext = [System.IO.Path]::GetExtension($BatPath)
if ($ext -ieq '.bat' -or $ext -ieq '.cmd') {
    $batDir = Split-Path -Parent $BatPath
    if ($batDir -and (Test-Path -LiteralPath $batDir)) {
        Set-Location -LiteralPath $batDir
    }
}

$rc = $null
try {
    # cmd.exe is the correct host for a .bat. Build a single command string and
    # merge the child's stderr into stdout INSIDE cmd ("... 2>&1") rather than at
    # the PowerShell level.
    #
    # Why the merge happens in cmd, not via PowerShell's `2>&1` (the v2.21.1 red-
    # text bug): when PowerShell's redirection captures a native command's stderr,
    # it wraps each stderr line in an ErrorRecord and renders it RED with a scary
    # ``NativeCommandError``/``RemoteException`` block — even for purely
    # informational stderr lines. `py -m venv` on a junctioned path (C:\claude ->
    # F:\claude) prints a benign "Actual environment location may have moved..."
    # notice to stderr, which then looked like a crash. Merging in cmd makes
    # PowerShell see one plain stdout text stream — no ErrorRecords, no red, no
    # wrapper — while the transcript still captures stdout+stderr in full.
    #
    # Quote the target path; append each arg quoted. The combined string is
    # handed to one `cmd /c` so spaced paths/args survive. Embedded double-quotes
    # are doubled ("") — cmd's literal-quote escape inside a quoted token — so an
    # arg like foo"bar can't break the command string (gemini MEDIUM PR #73).
    $quote = { param($s) '"' + ($s -replace '"', '""') + '"' }
    $cmdLine = (& $quote $BatPath)
    foreach ($a in $BatArgs) { $cmdLine += ' ' + (& $quote $a) }
    $cmdLine += ' 2>&1'

    # Exit-code capture (code-review HIGH #2): on Windows PowerShell 5.1
    # ``$LASTEXITCODE`` read AFTER a pipeline whose LAST element is a cmdlet
    # (Tee-Object) is unreliable. Capture cmd's real code from INSIDE the pipeline
    # (a foreach -End block) so it's correct on both PS5.1 and PS7.
    & $env:ComSpec /c $cmdLine |
        Tee-Object -FilePath $TranscriptPath |
        ForEach-Object {
            $_                       # pass the line through (preserves live console)
        } -End {
            $script:rc = $LASTEXITCODE
        }
    # Belt-and-suspenders: if the -End block didn't capture (empty output edge
    # case), fall back to the post-pipeline value.
    if ($null -eq $rc) { $rc = $LASTEXITCODE }
} catch {
    # If the tee pipeline INFRASTRUCTURE itself fails (cmd.exe missing, disk
    # full on the transcript write, locked-down ExecutionPolicy, permission
    # denied on .launcher_state\), surface it and return the DISTINCT sentinel
    # exit code 3 so the caller can tell "tee broke" from "the GUI exited N"
    # and fall through to a direct (un-teed) launch — the transcript is
    # best-effort and must NEVER block the launch (code-review HIGH).
    Write-Host "  [transcript] tee failed: $($_.Exception.Message)"
    $rc = 3
}

# Prune AFTER the run so this run's own transcript is included in the newest-5.
Invoke-TranscriptPrune

if ($null -eq $rc) { $rc = 0 }
exit $rc
