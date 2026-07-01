# Reinstall + verify the AgentOS_CognitiveLoop scheduled task on Windows.
#
# Re-registering an admin-owned task requires an ELEVATED shell (a non-elevated
# schtasks /Create returns "Access denied"). Run this from an elevated terminal
# (or right-click > Run as administrator) after pulling a new m3-memory.
#
# It delegates BOTH install and verification to install_schedules.py (the single
# source of truth) — the only Windows-specific thing left here is elevation.
# macOS/Linux don't need this script: `install_schedules.py --add cognitive-loop`
# installs the launchd/systemd service directly (no elevation quirk).
#
# Compatible with Windows PowerShell 5.1 and PowerShell 7+ (no 7-only syntax).

param(
    # Repo root. Auto-resolves from this script's location (bin/..), overridable.
    [string]$Repo = (Split-Path -Parent $PSScriptRoot),
    # Which schedule to (re)install. 'cognitive-loop' by default; 'all' for every task.
    [string]$Task = 'cognitive-loop'
)

$ErrorActionPreference = 'Stop'

$installer = Join-Path $Repo 'bin\install_schedules.py'
if (-not (Test-Path $installer)) {
    throw "install_schedules.py not found under $Repo — pass -Repo <path-to-m3-memory>."
}

# Prefer the repo venv; fall back to whatever 'python' resolves to.
$py = Join-Path $Repo '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "No python found (no venv at $py and no 'python' on PATH)." }
    $py = $cmd.Source
}

Write-Host "Installing scheduled task '$Task'..." -ForegroundColor Cyan
& $py $installer --add $Task
if ($LASTEXITCODE -ne 0) { throw "installer exited $LASTEXITCODE" }

Write-Host "`nVerifying..." -ForegroundColor Cyan
& $py $installer --verify $Task
$verifyExit = $LASTEXITCODE

if ($verifyExit -eq 0) {
    Write-Host "`nPASS: task registered and matches spec." -ForegroundColor Green
} else {
    Write-Host "`nFAIL: verification did not pass (see above)." -ForegroundColor Red
}
exit $verifyExit
