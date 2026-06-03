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

# Run from the .bat's own directory so a relative %~dp0 / cmd CWD is always
# valid (a bad inherited CWD is what prints "The system cannot find the path
# specified." to the console).
$batDir = Split-Path -Parent $BatPath
if ($batDir -and (Test-Path -LiteralPath $batDir)) {
    Set-Location -LiteralPath $batDir
}

try {
    # cmd.exe is the correct host for a .bat. Build the arg list explicitly so
    # spaced paths/args survive (no manual quote concatenation). Tee-Object
    # mirrors combined stdout+stderr to the file while passing it through to the
    # console, so the window shows everything live.
    & cmd.exe /c $BatPath @BatArgs 2>&1 |
        Tee-Object -FilePath $TranscriptPath
    $rc = $LASTEXITCODE
} catch {
    # If the tee pipeline itself fails, surface the error but don't crash the
    # launch — return non-zero so the caller can fall back to a direct run.
    Write-Host "  [transcript] tee failed: $($_.Exception.Message)"
    $rc = 1
}

if ($null -eq $rc) { $rc = 0 }
exit $rc
