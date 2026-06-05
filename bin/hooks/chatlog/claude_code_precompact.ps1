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

$ErrorActionPreference = "Continue"
$result = & $py @argsList 2>&1
$exitCode = $LASTEXITCODE
$ErrorActionPreference = "Stop"

# Parse result to check if anything was written
$written = 0
$ingestError = $null
try {
    # Extract just the JSON object from output (ingest may mix log lines + JSON)
    $jsonLine = ($result | Out-String) -split "`n" | Where-Object { $_ -match '^\s*\{' } | Select-Object -Last 1
    $resultJson = $jsonLine | ConvertFrom-Json -ErrorAction Stop
    $written = [int]($resultJson.written)
    $ingestError = $resultJson.error
} catch {
    $ingestError = "m3 ingest failed or unreachable"
}

# Scream if m3 failed to capture (0 written or error)
if ($written -eq 0 -or -not [string]::IsNullOrWhiteSpace($ingestError)) {
    $ts = Get-Date -Format "yyyy-MM-dd-HH-mm-ss"
    $fallbackDir = Join-Path $env:USERPROFILE ".claude"
    if (-not (Test-Path $fallbackDir)) { New-Item -ItemType Directory -Force $fallbackDir | Out-Null }
    $fallbackFile = Join-Path $fallbackDir "m3_unsaved_chatlog_$ts.md"

    $reason = if (-not [string]::IsNullOrWhiteSpace($ingestError)) { $ingestError } else { "0 rows written (m3 may be down or unreachable)" }

    $content = @"
# ⚠️ M3 CHATLOG NOT SAVED — $ts

**Event:** $eventName
**Session:** $sessionId
**Transcript path:** $transcript
**Reason:** $reason

This session's chatlog was NOT captured by m3-memory.
To recover: run the ingest manually against the transcript path above.

    python bin/chatlog_ingest.py --format claude-code --transcript-path "$transcript" --variant $variant
"@

    $content | Out-File -FilePath $fallbackFile -Encoding utf8

    $banner = @"

╔══════════════════════════════════════════════════════════════════╗
║  🚨 M3 CHATLOG NOT SAVED — SESSION CONTEXT WILL BE LOST 🚨      ║
║                                                                  ║
║  Reason : $($reason.PadRight(58).Substring(0,58))  ║
║  Saved  : $($fallbackFile.PadRight(58).Substring(0,58))  ║
║                                                                  ║
║  Fix: restart m3 MCP, then re-run ingest on transcript above.   ║
╚══════════════════════════════════════════════════════════════════╝
"@
    Write-Host $banner -ForegroundColor Red
}

exit $exitCode
