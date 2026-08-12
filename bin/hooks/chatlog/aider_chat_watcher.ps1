# Aider chat-history watcher → m3-memory chat log ingest (Windows).
# Aider writes to .aider.chat.history.md in the repo root; we tail it.
# Usage: .\aider_chat_watcher.ps1 [<repo_root>]
# Default repo_root = current working directory.

param([string]$RepoRoot = (Get-Location))

$here = $PSScriptRoot
$base = Resolve-Path (Join-Path $here "..\..\..")
# See claude_code_precompact.ps1: Test-Path alone accepts a dependency-less
# venv that then dies at `import httpx`, silently killing capture.
function Test-M3Python($candidate) {
    if (-not $candidate) { return $false }
    if ($candidate -ne "python" -and -not (Test-Path $candidate)) { return $false }
    try {
        & $candidate -c "import httpx" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch { return $false }
}

$py = $null
foreach ($cand in @(
    (Join-Path $base ".venv\Scripts\python.exe"),
    "$env:USERPROFILE\pipx\venvs\m3-memory\Scripts\python.exe",
    "$env:LOCALAPPDATA\pipx\pipx\venvs\m3-memory\Scripts\python.exe",
    $(if ($env:PIPX_HOME) { Join-Path $env:PIPX_HOME "venvs\m3-memory\Scripts\python.exe" }),
    $env:M3_PYTHON
)) {
    if (Test-M3Python $cand) { $py = $cand; break }
}

if (-not $py) {
    $py = "python"
    if (-not (Test-M3Python $py)) {
        Write-Warning "aider_chat_watcher: no python with httpx found; trying '$py' anyway"
    }
}

& $py (Join-Path $base "bin\chatlog_ingest.py") --format aider --watch $RepoRoot
exit $LASTEXITCODE
