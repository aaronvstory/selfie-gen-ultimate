<#
.SYNOPSIS
    SelfieGen — safe CUDA Toolkit cleanup (Windows). Fixes "rPPG runs on CPU".

.DESCRIPTION
    Run this ONLY if SelfieGen's rPPG keeps falling back to CPU (slow, minutes
    per iteration) even on a fresh install. It removes an OLD system CUDA
    *Toolkit* (e.g. CUDA v11.8) from your PATH / environment variables so the
    app's bundled CUDA can take over.

    It is SAFE:
      * Backs everything up to your Desktop FIRST (fully reversible).
      * Shows you the exact planned changes and asks before doing anything.
      * Removes ONLY CUDA Toolkit references — never the NVIDIA graphics driver.
      * Re-adds nvidia-smi so it keeps working.
      * Also clears the CuPy kernel cache so the GPU re-compiles cleanly.

    Your SelfieGen install / its venv is NOT touched — only Windows-wide CUDA
    Toolkit references. After running, REBOOT and re-launch SelfieGen.

    You do NOT need to type anything in a terminal — just double-click the .bat
    that sits next to this file.
#>

[CmdletBinding()]
param(
    [switch]$Yes,     # skip the confirmation prompt (advanced)
    [switch]$DryRun   # show what WOULD change, change nothing
)

$ErrorActionPreference = 'Stop'

# --- Self-elevate to Administrator (needed to edit system PATH) -------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host 'Asking Windows for Administrator permission (approve the popup)...' -ForegroundColor Yellow
    $argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSCommandPath`"")
    if ($Yes)    { $argList += '-Yes' }
    if ($DryRun) { $argList += '-DryRun' }
    try {
        Start-Process -FilePath 'powershell.exe' -ArgumentList $argList -Verb RunAs
    } catch {
        Write-Host 'Could not get Administrator permission.' -ForegroundColor Red
        Write-Host 'Right-click "Run CUDA Cleanup.bat" and choose "Run as administrator".' -ForegroundColor Red
        Read-Host 'Press Enter to close'
    }
    return
}

Write-Host ''
Write-Host '==============================================================' -ForegroundColor Cyan
Write-Host '  SelfieGen CUDA cleanup' -ForegroundColor Cyan
Write-Host '  Keeps your NVIDIA driver. Backs up first. Reversible.' -ForegroundColor Cyan
Write-Host '==============================================================' -ForegroundColor Cyan
Write-Host ''
Write-Host 'This removes an OLD CUDA Toolkit from PATH so SelfieGen GPU works.' -ForegroundColor Gray
Write-Host 'It does NOT remove your graphics driver and does NOT touch SelfieGen.' -ForegroundColor Gray
Write-Host ''

# --- 1. Back up CUDA env vars + both PATH scopes to the Desktop -------------
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

# --- 2. Compute the planned changes (don't apply yet) ----------------------
$isCudaToolkitPath = {
    param($entry)
    ($entry -match '\\NVIDIA GPU Computing Toolkit\\CUDA\\') -or
    ($entry -match '\\NVIDIA GPU Computing Toolkit\\CUDA$')
}

$plannedEnvRemovals  = @()
$plannedPathRemovals = @{}

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
Write-Host '  Clear the CuPy kernel cache (forces a clean GPU re-compile).'
Write-Host ''

$nothingToDo = ($plannedEnvRemovals.Count -eq 0 -and
                $plannedPathRemovals['User'].Count -eq 0 -and
                $plannedPathRemovals['Machine'].Count -eq 0)

if ($nothingToDo) {
    Write-Host 'No CUDA Toolkit references found in your environment.' -ForegroundColor Green
    Write-Host 'The CuPy cache will still be cleared (harmless), then you can re-test.' -ForegroundColor Gray
}

if ($DryRun) {
    Write-Host ''
    Write-Host 'DRY RUN: nothing was changed.' -ForegroundColor Yellow
    Read-Host 'Press Enter to close'
    return
}

# --- 3. CONFIRMATION (the explicit step you asked for) ---------------------
if (-not $Yes) {
    Write-Host ''
    Write-Host 'Type  YES  to apply these changes (anything else cancels):' -ForegroundColor Yellow
    $ans = Read-Host '> '
    if ($ans.Trim().ToUpper() -ne 'YES') {
        Write-Host 'Cancelled. Nothing was changed.' -ForegroundColor Yellow
        Read-Host 'Press Enter to close'
        return
    }
}

# --- 4. Apply env-var + PATH removals --------------------------------------
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
            if (& $isCudaToolkitPath $e) { continue }
            if (-not $kept.Contains($e)) { $kept.Add($e) }
        }
        [Environment]::SetEnvironmentVariable('Path', ($kept -join ';'), $scope)
    }
}

# Keep nvidia-smi working. Don't rely on the default install path — a custom
# driver install (D:\, corporate image) puts nvidia-smi elsewhere. Scan EVERY
# directory currently resolvable for nvidia-smi.exe (PATH + the common default)
# and make sure at least one stays on the Machine PATH (code-review MEDIUM,
# PR #73). The CUDA Toolkit removal above never touches a real nvidia-smi dir
# (that lives under NVIDIA Corporation, not NVIDIA GPU Computing Toolkit\CUDA),
# so this is belt-and-suspenders, robust to non-default installs.
$nvSmiDirs = New-Object System.Collections.Generic.List[string]
$searchDirs = @()
foreach ($scope in @('User', 'Machine')) {
    $p = [Environment]::GetEnvironmentVariable('Path', $scope)
    if ($p) { $searchDirs += ($p -split ';') }
}
$searchDirs += 'C:\Program Files\NVIDIA Corporation\NVSMI'
foreach ($d in $searchDirs) {
    $d = $d.Trim()
    if ($d -and (Test-Path -LiteralPath (Join-Path $d 'nvidia-smi.exe')) -and (-not $nvSmiDirs.Contains($d))) {
        $nvSmiDirs.Add($d)
    }
}
$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$machineEntries = @($machinePath -split ';' | ForEach-Object { $_.Trim() })
foreach ($d in $nvSmiDirs) {
    if ($machineEntries -notcontains $d) {
        Write-Host "Re-adding NVIDIA driver dir so nvidia-smi keeps working: $d"
        $machinePath = ($machinePath.TrimEnd(';') + ';' + $d)
        [Environment]::SetEnvironmentVariable('Path', $machinePath, 'Machine')
        $machineEntries += $d
    }
}

# --- 5. Clear the CuPy kernel cache (stale cubins from the old toolkit) -----
$cupyCache = Join-Path $env:USERPROFILE '.cupy\kernel_cache'
if (Test-Path -LiteralPath $cupyCache) {
    try {
        Remove-Item -LiteralPath $cupyCache -Recurse -Force -ErrorAction Stop
        Write-Host "Cleared CuPy kernel cache: $cupyCache"
    } catch {
        Write-Host "Could not fully clear CuPy cache (harmless): $cupyCache" -ForegroundColor DarkGray
    }
} else {
    Write-Host 'No CuPy kernel cache to clear (fine).'
}

Write-Host ''
Write-Host 'DONE.' -ForegroundColor Green
Write-Host '----------------------------------------------------------------' -ForegroundColor Cyan
Write-Host 'NEXT STEPS:' -ForegroundColor Cyan
Write-Host '  1. RESTART Windows now (so the change takes effect).'
Write-Host '  2. After reboot, extract a FRESH SelfieGen to a NEW folder.'
Write-Host '  3. Launch it and run a short rPPG job again.'
Write-Host ''
Write-Host '  (Your SelfieGen install/venv was NOT changed by this tool.)' -ForegroundColor Gray
Write-Host "  Undo file (if ever needed): $backup" -ForegroundColor DarkGray
Write-Host '----------------------------------------------------------------' -ForegroundColor Cyan
Read-Host 'Press Enter to close'
