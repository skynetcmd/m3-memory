# Aider chat-history watcher → m3-memory chat log ingest (Windows).
# Aider writes to .aider.chat.history.md in the repo root; we tail it.
# Usage: .\aider_chat_watcher.ps1 [<repo_root>]
# Default repo_root = current working directory.

param([string]$RepoRoot = (Get-Location))

$here = $PSScriptRoot
$base = Resolve-Path (Join-Path $here "..\..\..")
$py = Join-Path $base ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

& $py (Join-Path $base "bin\chatlog_ingest.py") --format aider --watch $RepoRoot
exit $LASTEXITCODE
