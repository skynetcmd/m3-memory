# OpenCode session-end hook → m3-memory chat log ingest (Windows).
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
        Write-Warning "opencode_session_end: no python with httpx found; trying '$py' anyway"
    }
}

# ⚠ --transcript-path IS REQUIRED and was missing, so this exited 2 on every
# fire and OpenCode capture never ran. OpenCode writes no transcript FILE: it
# keeps sessions in opencode.db (v1.2.0+) or a legacy per-message JSON tree, so
# point ingest at the STORE and let the parser resolve the layout.
# "auto" asks chatlog_ingest to locate the store. The per-OS candidate list
# lives there, in ONE place, rather than being re-derived in each shell wrapper
# — which is how two platforms drift apart (DESIGN §1, §10a).
$input | & $py (Join-Path $base "bin\chatlog_ingest.py") --format opencode --transcript-path auto --variant session_end
exit $LASTEXITCODE
