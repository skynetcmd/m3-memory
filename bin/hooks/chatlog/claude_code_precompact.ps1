# Claude Code PreCompact / Stop hook -> m3-memory chat log ingest (Windows).
#
# Envelope on stdin (per https://code.claude.com/docs/en/hooks.md):
#   { "session_id": "...", "transcript_path": "...",
#     "cwd": "...", "hook_event_name": "PreCompact" | "Stop", ... }
#
# We forward transcript_path + session_id as CLI flags and tag the rows with a
# variant derived from hook_event_name so later queries can distinguish
# "pre_compact" captures from per-turn "stop" captures.

$ErrorActionPreference = "Stop"

# Resolve repo root: $M3_HOME wins, else script-relative (..\..\..).
$base = if ($env:M3_HOME) { $env:M3_HOME } else { Resolve-Path (Join-Path $PSScriptRoot "..\..\..") }

if (-not (Test-Path (Join-Path $base "bin\chatlog_ingest.py"))) {
    Write-Error "claude_code_precompact: could not find bin\chatlog_ingest.py under '$base'. Set M3_HOME to the m3-memory repo root."
    exit 1
}

$py = Join-Path $base ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

# Read stdin: prefer the automatic $input enumerator (works for PowerShell
# pipelines), fall back to [Console]::In for a non-PowerShell parent (real
# stdin from the host CLI process).
$raw = ($input | Out-String)
if ([string]::IsNullOrWhiteSpace($raw)) {
    $raw = [Console]::In.ReadToEnd()
}
if ([string]::IsNullOrWhiteSpace($raw)) {
    Write-Error "claude_code_precompact: empty stdin envelope"
    exit 1
}

try {
    $env = $raw | ConvertFrom-Json
} catch {
    Write-Error "claude_code_precompact: malformed JSON envelope: $_"
    exit 1
}

$transcript = $env.transcript_path
$sessionId  = $env.session_id
$eventName  = $env.hook_event_name

if ([string]::IsNullOrWhiteSpace($transcript)) {
    Write-Error "claude_code_precompact: envelope missing transcript_path"
    exit 1
}

$variant = switch ($eventName) {
    "PreCompact" { "pre_compact" }
    "Stop"       { "stop" }
    default      { "claude_code" }
}

$argsList = @(
    (Join-Path $base "bin\chatlog_ingest.py"),
    "--format", "claude-code",
    "--transcript-path", $transcript,
    "--variant", $variant
)
if (-not [string]::IsNullOrWhiteSpace($sessionId)) {
    $argsList += @("--session-id", $sessionId)
}

& $py @argsList
exit $LASTEXITCODE
