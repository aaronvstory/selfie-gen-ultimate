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

# Run from the .bat's own directory so a relative %~dp0 / cmd CWD is always
# valid (a bad inherited CWD is what prints "The system cannot find the path
# specified." to the console).
$batDir = Split-Path -Parent $BatPath
if ($batDir -and (Test-Path -LiteralPath $batDir)) {
    Set-Location -LiteralPath $batDir
}

$rc = $null
try {
    # cmd.exe is the correct host for a .bat. Build the arg list explicitly so
    # spaced paths/args survive (no manual quote concatenation). Tee-Object
    # mirrors combined stdout+stderr to the file while passing it through to the
    # console, so the window shows everything live.
    #
    # Exit-code capture (code-review HIGH #2): on Windows PowerShell 5.1
    # ``$LASTEXITCODE`` read AFTER a pipeline whose LAST element is a cmdlet
    # (Tee-Object) is unreliable — it can report 0 even when cmd.exe failed,
    # silently hiding a GUI crash from the caller's ``set TEE_RC=%errorlevel%``.
    # Capture cmd's real code from INSIDE the pipeline (a foreach-side scriptblock
    # that records $LASTEXITCODE the instant the native command upstream
    # finishes), so it's correct on both PS5.1 and PS7.
    & cmd.exe /c $BatPath @BatArgs 2>&1 |
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
