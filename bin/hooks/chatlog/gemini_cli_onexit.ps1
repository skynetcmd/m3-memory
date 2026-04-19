# Gemini CLI SessionEnd hook -> m3-memory chat log ingest (Windows).
#
# Envelope on stdin (per gemini-cli docs/hooks/reference.md, Base input schema):
#   { "session_id": "...", "transcript_path": "...",
#     "cwd": "...", "hook_event_name": "SessionEnd", "timestamp": "...",
#     "reason": "exit" | "clear" | "logout" | "prompt_input_exit" | "other" }
#
# Gemini's SessionEnd is fire-and-forget (CLI does not wait), and historically
# fired twice on exit (gemini-cli#18019, since fixed). Idempotency is handled
# downstream by the per-session UUID cursor in chatlog_ingest.py.

$ErrorActionPreference = "Stop"

# Resolve repo root: $M3_HOME wins, else script-relative (..\..\..).
$base = if ($env:M3_HOME) { $env:M3_HOME } else { Resolve-Path (Join-Path $PSScriptRoot "..\..\..") }

if (-not (Test-Path (Join-Path $base "bin\chatlog_ingest.py"))) {
    Write-Error "gemini_cli_onexit: could not find bin\chatlog_ingest.py under '$base'. Set M3_HOME to the m3-memory repo root."
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
    Write-Error "gemini_cli_onexit: empty stdin envelope"
    exit 1
}

try {
    $env = $raw | ConvertFrom-Json
} catch {
    Write-Error "gemini_cli_onexit: malformed JSON envelope: $_"
    exit 1
}

$transcript = $env.transcript_path
$sessionId  = $env.session_id
$reason     = $env.reason

if ([string]::IsNullOrWhiteSpace($transcript)) {
    Write-Error "gemini_cli_onexit: envelope missing transcript_path"
    exit 1
}

$variant = if ([string]::IsNullOrWhiteSpace($reason)) { "session_end" } else { "session_end_$reason" }

$argsList = @(
    (Join-Path $base "bin\chatlog_ingest.py"),
    "--format", "gemini-cli",
    "--transcript-path", $transcript,
    "--variant", $variant
)
if (-not [string]::IsNullOrWhiteSpace($sessionId)) {
    $argsList += @("--session-id", $sessionId)
}

& $py @argsList
exit $LASTEXITCODE
