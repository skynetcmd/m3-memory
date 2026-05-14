<#
.SYNOPSIS
    Repair m3-memory Windows scheduled tasks and trigger the periodic ones.

.DESCRIPTION
    Run this in an ELEVATED (Administrator) PowerShell window.

    1. Runs `install_schedules.py --repair`, which deletes the old
       cmd.exe-wrapped tasks and recreates all 7 with the new no-window
       definitions (python.exe registered directly, --log-file args,
       --background on the cognitive loop).
    2. Manually triggers the 6 periodic tasks so you can confirm NO console
       window appears.
    3. Reports each task's state and last-run result.

    AgentOS_CognitiveLoop is registered but NOT triggered here -- it is an
    ONSTART continuous daemon and will start on next login. Start it manually
    later (Start-ScheduledTask -TaskName AgentOS_CognitiveLoop) once you have
    confirmed the periodic tasks behave.

.NOTES
    LastTaskResult: 0 = clean exit, 267009 = still running (normal for the
    drain/sweep tasks if there is a queue to process).
#>

$ErrorActionPreference = 'Stop'

# --- Resolve repo root (parent of this script's bin/ dir) -------------------
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
Write-Host "Repo root: $RepoRoot`n"

# --- Pick the venv python if present ---------------------------------------
$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$Python = if (Test-Path $VenvPython) { $VenvPython } else { 'python' }
Write-Host "Python: $Python`n"

# --- 1. Repair: delete old tasks, recreate all 7 ---------------------------
Write-Host '--- install_schedules.py --repair ---' -ForegroundColor Cyan
& $Python (Join-Path $RepoRoot 'bin\install_schedules.py') --repair
if ($LASTEXITCODE -ne 0) {
    Write-Host "install_schedules.py exited $LASTEXITCODE -- stopping." -ForegroundColor Red
    exit $LASTEXITCODE
}

# --- 2. Trigger the 6 periodic tasks now -----------------------------------
Write-Host "`n--- Triggering periodic tasks now (watch for windows) ---`n" -ForegroundColor Cyan
$Periodic = @(
    'AgentOS_WeeklyAuditor',
    'AgentOS_HourlySync',
    'AgentOS_Maintenance',
    'AgentOS_SecretRotator',
    'AgentOS_ChatlogEmbedSweep',
    'AgentOS_ObservationDrain'
)
foreach ($t in $Periodic) {
    # schtasks /Run is used instead of Start-ScheduledTask because the
    # ScheduledTasks CIM module fails to initialize in some shells
    # (HRESULT 0x80070002). schtasks.exe is the reliable path.
    $r = schtasks /Run /TN $t 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  started $t"
    } else {
        Write-Host "  FAILED to start $t : $r" -ForegroundColor Red
    }
}

Start-Sleep -Seconds 3

# --- 3. Report -------------------------------------------------------------
# Using schtasks /Query (not Get-ScheduledTask) for the same CIM-module
# reliability reason noted above.
Write-Host "`n--- Task status + last run result ---" -ForegroundColor Cyan
Write-Host "  (Last Result: 0 = OK, 267009 = still running)`n"
$All = 'AgentOS_WeeklyAuditor','AgentOS_HourlySync','AgentOS_Maintenance',
       'AgentOS_SecretRotator','AgentOS_ChatlogEmbedSweep','AgentOS_ObservationDrain',
       'AgentOS_CognitiveLoop'
foreach ($t in $All) {
    $info = schtasks /Query /TN $t /FO LIST /V 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host ("  {0,-26} NOT REGISTERED" -f $t) -ForegroundColor Yellow
        continue
    }
    $status = ($info | Select-String '^Status:').ToString().Split(':',2)[1].Trim()
    $last   = ($info | Select-String '^Last Result:').ToString().Split(':',2)[1].Trim()
    $run    = ($info | Select-String '^Last Run Time:').ToString().Split(':',2)[1].Trim()
    Write-Host ("  {0,-26} status={1,-12} lastResult={2,-10} lastRun={3}" -f $t, $status, $last, $run)
}

Write-Host "`nDone. AgentOS_CognitiveLoop was registered but not triggered (ONSTART daemon)." -ForegroundColor Green
