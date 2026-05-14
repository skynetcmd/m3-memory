<#
.SYNOPSIS
    Fix m3-memory's Windows scheduled tasks so they stop opening focus-stealing
    console windows.

.DESCRIPTION
    Older m3-memory installs registered scheduled tasks that ran through
    cmd.exe, which popped a console window on screen every time a task fired
    (every 15-30 minutes). This script re-registers all m3-memory scheduled
    tasks with the current, windowless definitions.

    Just run it. If it is not already running as Administrator it will
    re-launch itself elevated (you will see a UAC prompt). No arguments needed.

    What it does:
      1. Shows the current m3-memory tasks (before).
      2. Runs `install_schedules.py --repair` — deletes the old tasks and
         recreates all 7 with the windowless definitions.
      3. Shows the tasks again (after).

    It does NOT force-run any task — they simply fire on their normal schedule
    from now on, without a window.

    macOS / Linux users: you do not have this problem (cron jobs never draw a
    window). Just run `python bin/install_schedules.py --add all` normally.

.NOTES
    Safe to run more than once — --repair is idempotent.
#>

# --- Self-elevate if not already running as Administrator ------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()
).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

if (-not $isAdmin) {
    Write-Host "Not running as Administrator — re-launching elevated (accept the UAC prompt)..." -ForegroundColor Yellow
    try {
        Start-Process -FilePath 'powershell.exe' -Verb RunAs -ArgumentList @(
            '-NoProfile',
            '-ExecutionPolicy', 'Bypass',
            '-File', "`"$PSCommandPath`""
        )
    } catch {
        Write-Host "Could not elevate automatically: $_" -ForegroundColor Red
        Write-Host "Open an Administrator PowerShell and run this script again." -ForegroundColor Red
        exit 1
    }
    # The elevated copy does the work; this non-elevated copy is done.
    exit 0
}

$ErrorActionPreference = 'Stop'

# --- Resolve repo root (parent of this script's bin/ dir) ------------------
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot
Write-Host "m3-memory scheduled-task fix" -ForegroundColor Cyan
Write-Host "Repo root: $RepoRoot`n"

# --- Pick the venv python if present ---------------------------------------
$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$Python = if (Test-Path $VenvPython) { $VenvPython } else { 'python' }
if ($Python -eq 'python') {
    Write-Host "WARNING: .venv not found — using system 'python' on PATH.`n" -ForegroundColor Yellow
}

# --- Helper: print current task status -------------------------------------
function Show-TaskStatus {
    param([string]$Heading)
    Write-Host "--- $Heading ---" -ForegroundColor Cyan
    $names = 'AgentOS_WeeklyAuditor','AgentOS_HourlySync','AgentOS_Maintenance',
             'AgentOS_SecretRotator','AgentOS_ChatlogEmbedSweep',
             'AgentOS_ObservationDrain','AgentOS_CognitiveLoop'
    $any = $false
    foreach ($t in $names) {
        # schtasks /Query is used instead of Get-ScheduledTask: the
        # ScheduledTasks CIM module fails to initialize in some shells
        # (HRESULT 0x80070002). schtasks.exe is the reliable path.
        $info = schtasks /Query /TN $t /FO LIST /V 2>&1
        if ($LASTEXITCODE -ne 0) { continue }
        $any = $true
        $tr = ($info | Select-String '^Task To Run:').ToString().Split(':',2)[1].Trim()
        $usesCmd = if ($tr -match '(?i)cmd\.exe') { 'cmd.exe (OLD - flashes a window)' }
                   elseif ($tr -match '(?i)pythonw\.exe') { 'pythonw.exe (FIXED - no window)' }
                   else { 'python.exe' }
        Write-Host ("  {0,-26} {1}" -f $t, $usesCmd)
    }
    if (-not $any) { Write-Host "  (no m3-memory tasks registered yet)" }
    Write-Host ""
}

Show-TaskStatus -Heading "Before"

# --- Re-register all tasks via the installer -------------------------------
Write-Host "--- Running install_schedules.py --repair ---" -ForegroundColor Cyan
& $Python (Join-Path $RepoRoot 'bin\install_schedules.py') --repair
if ($LASTEXITCODE -ne 0) {
    Write-Host "`ninstall_schedules.py exited $LASTEXITCODE — fix did not complete." -ForegroundColor Red
    exit $LASTEXITCODE
}
Write-Host ""

Show-TaskStatus -Heading "After"

Write-Host "Done. The tasks will now run on their normal schedule with no console window." -ForegroundColor Green
Write-Host "(AgentOS_CognitiveLoop starts on next login — it is an ONSTART daemon.)" -ForegroundColor Green
