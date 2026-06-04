<#
.SYNOPSIS
    SelfieGen — safe CUDA Toolkit PATH / env cleanup (Windows).

.DESCRIPTION
    Removes ONLY old CUDA *Toolkit* environment variables and PATH entries that
    can poison CuPy / NVRTC header discovery (e.g. an old CUDA v11.8 install).

    It does NOT touch:
      * the NVIDIA graphics driver
      * nvidia-smi (it is re-added if found, so it keeps working)
      * anything outside CUDA Toolkit references

    Everything is BACKED UP to your Desktop first, and nothing is changed until
    you confirm. You can undo by restoring values from the backup file.

    Normally you should NOT need this — the SelfieGen app (v2.23.4+) already
    isolates itself from a system CUDA Toolkit. Run this only if rPPG still
    falls back to CPU after a fresh v2.23.4 install.

.NOTES
    Safe to run more than once. Reboot afterward so open processes pick up the
    cleaned environment.
#>

[CmdletBinding()]
param(
    # Skip the interactive confirmation (for advanced/automated use).
    [switch]$Yes,
    # Show what WOULD change without modifying anything.
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Self-elevate: re-launch as Administrator if we aren't already (Machine-scope
# env edits require it). Re-passes -Yes / -DryRun through.
# ---------------------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host 'Requesting Administrator privileges (needed to edit system PATH)...' -ForegroundColor Yellow
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    if ($Yes)    { $argList += '-Yes' }
    if ($DryRun) { $argList += '-DryRun' }
    try {
        Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs
    } catch {
        Write-Host 'Could not elevate. Right-click this file and choose "Run with PowerShell" as an administrator.' -ForegroundColor Red
        Read-Host 'Press Enter to exit'
    }
    return
}

Write-Host ''
Write-Host '==============================================================' -ForegroundColor Cyan
Write-Host '  SelfieGen — CUDA Toolkit PATH / env cleanup' -ForegroundColor Cyan
Write-Host '  (keeps your NVIDIA driver + nvidia-smi; backs up first)' -ForegroundColor Cyan
Write-Host '==============================================================' -ForegroundColor Cyan
Write-Host ''

# ---------------------------------------------------------------------------
# 1. Back up current CUDA env vars + both PATH scopes to the Desktop.
# ---------------------------------------------------------------------------
$stamp  = Get-Date -Format 'yyyyMMdd-HHmmss'
$backup = Join-Path ([Environment]::GetFolderPath('Desktop')) "cuda-env-backup-$stamp.txt"

$cudaNameMatch = {
    param($name)
    ($name -like 'CUDA_PATH*') -or
    ($name -in @('CUDA_PATH', 'CUDA_HOME', 'CUDA_ROOT', 'CUDA_BIN_PATH', 'CUDA_INC_PATH'))
}

"=== CUDA env/PATH backup taken $stamp ===" | Out-File $backup -Encoding UTF8
foreach ($scope in @('User', 'Machine')) {
    "--- $scope CUDA vars ---" | Out-File $backup -Append -Encoding UTF8
    [Environment]::GetEnvironmentVariables($scope).GetEnumerator() |
        Where-Object { & $cudaNameMatch $_.Key } |
        ForEach-Object { "$($_.Key)=$($_.Value)" } |
        Out-File $backup -Append -Encoding UTF8
    "--- $scope PATH ---" | Out-File $backup -Append -Encoding UTF8
    [Environment]::GetEnvironmentVariable('Path', $scope) | Out-File $backup -Append -Encoding UTF8
}
Write-Host "Backup saved to:`n  $backup" -ForegroundColor Green
Write-Host ''

# ---------------------------------------------------------------------------
# 2. Compute (but don't yet apply) the changes: env vars to remove + PATH
#    entries to drop. A PATH entry is a "CUDA Toolkit" entry only if it points
#    INTO the NVIDIA GPU Computing Toolkit\CUDA tree. Driver/NVSMI paths are kept.
# ---------------------------------------------------------------------------
$isCudaToolkitPath = {
    param($entry)
    ($entry -match '\\NVIDIA GPU Computing Toolkit\\CUDA\\') -or
    ($entry -match '\\NVIDIA GPU Computing Toolkit\\CUDA$')
}

$plannedEnvRemovals  = @()
$plannedPathRemovals = @{}   # scope -> @(entries)

foreach ($scope in @('User', 'Machine')) {
    foreach ($key in @([Environment]::GetEnvironmentVariables($scope).Keys)) {
        if (& $cudaNameMatch $key) { $plannedEnvRemovals += "$scope`:$key" }
    }
    $path = [Environment]::GetEnvironmentVariable('Path', $scope)
    $drop = @()
    if (-not [string]::IsNullOrWhiteSpace($path)) {
        foreach ($entry in ($path -split ';')) {
            $e = $entry.Trim()
            if ($e -and (& $isCudaToolkitPath $e)) { $drop += $e }
        }
    }
    $plannedPathRemovals[$scope] = $drop
}

Write-Host 'Planned changes:' -ForegroundColor Cyan
if ($plannedEnvRemovals.Count -eq 0) {
    Write-Host '  (no CUDA env vars to remove)'
} else {
    Write-Host '  Remove these CUDA env vars:'
    $plannedEnvRemovals | ForEach-Object { Write-Host "    $_" }
}
foreach ($scope in @('User', 'Machine')) {
    if ($plannedPathRemovals[$scope].Count -eq 0) {
        Write-Host "  (no CUDA Toolkit entries in $scope PATH)"
    } else {
        Write-Host "  Remove from $scope PATH:"
        $plannedPathRemovals[$scope] | ForEach-Object { Write-Host "    $_" }
    }
}
Write-Host ''

if ($plannedEnvRemovals.Count -eq 0 -and
    $plannedPathRemovals['User'].Count -eq 0 -and
    $plannedPathRemovals['Machine'].Count -eq 0) {
    Write-Host 'Nothing to clean — your environment already has no CUDA Toolkit references.' -ForegroundColor Green
    Read-Host 'Press Enter to exit'
    return
}

if ($DryRun) {
    Write-Host 'DRY RUN: no changes were made.' -ForegroundColor Yellow
    Read-Host 'Press Enter to exit'
    return
}

if (-not $Yes) {
    $ans = Read-Host 'Apply these changes? (Y/N)'
    if ($ans -notin @('Y', 'y', 'yes', 'Yes')) {
        Write-Host 'Cancelled — nothing changed.' -ForegroundColor Yellow
        Read-Host 'Press Enter to exit'
        return
    }
}

# ---------------------------------------------------------------------------
# 3. Apply: remove env vars, rewrite PATH (dedup-preserving), keep nvidia-smi.
# ---------------------------------------------------------------------------
foreach ($scope in @('User', 'Machine')) {
    foreach ($key in @([Environment]::GetEnvironmentVariables($scope).Keys)) {
        if (& $cudaNameMatch $key) {
            Write-Host "Removing $scope env var: $key"
            [Environment]::SetEnvironmentVariable($key, $null, $scope)
        }
    }
    $path = [Environment]::GetEnvironmentVariable('Path', $scope)
    if (-not [string]::IsNullOrWhiteSpace($path)) {
        $kept = New-Object System.Collections.Generic.List[string]
        foreach ($entry in ($path -split ';')) {
            $e = $entry.Trim()
            if (-not $e) { continue }
            if (& $isCudaToolkitPath $e) { continue }       # drop CUDA Toolkit
            if (-not $kept.Contains($e)) { $kept.Add($e) }  # keep, dedup
        }
        [Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), $scope)
    }
}

# Re-add the NVIDIA driver's NVSMI dir if present, so nvidia-smi stays callable.
$nvSmiDir = 'C:\Program Files\NVIDIA Corporation\NVSMI'
if (Test-Path (Join-Path $nvSmiDir 'nvidia-smi.exe')) {
    $machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    if (($machinePath -split ';') -notcontains $nvSmiDir) {
        Write-Host "Re-adding NVIDIA driver NVSMI dir so nvidia-smi keeps working: $nvSmiDir"
        [Environment]::SetEnvironmentVariable('Path', ($machinePath.TrimEnd(';') + ';' + $nvSmiDir), 'Machine')
    }
}

Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
Write-Host 'NEXT STEPS:' -ForegroundColor Cyan
Write-Host '  1. RESTART Windows (so open programs pick up the cleaned environment).'
Write-Host '  2. After reboot, confirm the driver still works:   nvidia-smi'
Write-Host '  3. Extract a FRESH SelfieGen v2.23.4 to a NEW folder and run it.'
Write-Host ''
Write-Host "Backup (to undo): $backup" -ForegroundColor DarkGray
Read-Host 'Press Enter to exit'
