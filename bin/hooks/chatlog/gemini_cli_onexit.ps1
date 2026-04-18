# Gemini CLI session-end hook → m3-memory chat log ingest (Windows).
$here = $PSScriptRoot
$base = Resolve-Path (Join-Path $here "..\..\..")
$py = Join-Path $base ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$input | & $py (Join-Path $base "bin\chatlog_ingest.py") --format gemini-cli
exit $LASTEXITCODE
