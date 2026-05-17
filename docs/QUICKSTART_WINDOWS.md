# M3 Memory — Windows Quick Start

Get persistent memory + directory ingestion running on Windows in under five minutes. Works with Claude Code, Gemini CLI, OpenCode, and OpenClaw.

> No bash needed. Everything here is PowerShell.

---

## 1. Install

If you don't already have Python/pip installed (or aren't sure), run these three commands once in an **elevated PowerShell**:

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
winget install -e --id SQLite.SQLite
```

Then as your normal user, install m3 and start the smart setup:

```powershell
pip install m3-memory
m3 setup
```

`m3 setup` detects every agent on PATH, asks a handful of questions, and drives the rest end-to-end:

- system payload
- embedder (everything's bundled — no LM Studio, no Ollama, no internet, no GPU required)
- per-agent MCP wiring (Claude Code, Gemini CLI, OpenCode, OpenClaw)
- chatlog Stop + PreCompact hooks
- final `m3 doctor` verification

Restart your agent and you're done. The rest of this doc covers the features.

> **Have a GPU?** The wizard asks once whether to add GPU acceleration on top of the default embedder for ~10-50× faster embeddings (needs CUDA Toolkit + nvcc on PATH, or Vulkan SDK). You can also add it later with `m3 embedder install-gpu`.

> **Tool catalog stays small in your context.** m3 ships 87 MCP tools but groups them into 8 domains (memory, chatlog, files, entity, agent, tasks, conversations, admin). Only ~6 essentials load at MCP startup (~2,400 tokens vs ~16,100 if all 87 loaded eagerly). The agent pulls in a domain on demand — just say "load the files tools" and it does. Set `M3_TOOLS_LAZY=0` to disable.

> If `m3` isn't found after `pip install --user`, add `%APPDATA%\Python\Python312\Scripts` (substitute your Python version) to your user PATH. See [install_windows.md](install_windows.md#common-gotchas) for the exact recipe.

---

## 2. Connect M3 to your agent

If you ran `m3 setup` (step 1), every agent it detected on PATH is **already wired**. Restart the agent and the m3 MCP server is there. Skip to step 3.

If you skipped the wizard, or you're adding an agent later, here's the manual recipe per agent:

### Claude Code (recommended: plugin)

```
/plugin marketplace add skynetcmd/m3-memory
/plugin install m3@skynetcmd
```

Then `/plugin reload` (or restart Claude Code). The plugin auto-registers the MCP, wires the chatlog Stop + PreCompact hooks, and adds 15 `/m3:*` slash commands plus two curator subagents — confirm with `/m3:health`.

If you'd rather wire it by hand:

```powershell
claude mcp add memory m3
```

### Gemini CLI

`m3 setup` already wrote the entry into `%USERPROFILE%\.gemini\settings.json`. After install, just restart Gemini. That's it!

### OpenCode

Add to `opencode.json` (project root) or `%APPDATA%\opencode\opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "memory": { "type": "local", "command": ["m3"], "enabled": true }
  }
}
```

### OpenClaw

OpenClaw can't speak MCP natively. Run the bundled proxy on `localhost:9000` in the background and point OpenClaw's OpenAI endpoint there:

```powershell
Start-Process -WindowStyle Hidden `
  -FilePath python.exe `
  -ArgumentList "$env:USERPROFILE\m3-memory\bin\mcp_proxy.py" `
  -RedirectStandardOutput "$env:TEMP\mcp_proxy.log"

# Then set OpenClaw's base URL to:  http://localhost:9000/v1
```

---

## 3. Ingest a directory

Just tell your agent:

> ingest files at C:\Users\you\Documents\notes

That's it. The agent indexes every supported file under the path. You can also scope it ("...as the `notes` corpus") or ask for fact extraction ("...and extract facts inline").

### Supported file types

m3 chunks these formats natively:

| Filetype | Extensions | How it's chunked |
|---|---|---|
| Markdown / RST | `.md` `.markdown` `.mdx` `.rst` | By heading tree |
| PDF | `.pdf` | One leaf per page |
| Plain text | `.txt` `.log` | Semantic paragraph |
| Code | `.py` `.ts` `.js` `.rs` `.go` `.java` `.rb` `.php` `.c` `.h` `.cpp` `.cs` `.swift` `.kt` `.scala` `.ps1` `.sql` | Paragraph fallback |
| Config / data | `.toml` `.yaml` `.yml` `.json` `.jsonl` `.ini` `.cfg` `.conf` `.csv` `.tsv` | Paragraph fallback |
| Web / docs | `.html` `.htm` `.xml` `.epub` `.docx` `.tex` | Paragraph fallback |
| Notebooks | `.ipynb` | Paragraph fallback |

Binary files (images, archives, lock files) are skipped automatically. Files over 10 MiB are skipped unless you pass `--force-size`.

For images and audio: convert them to text first with your favorite tool (`tesseract` for images, `whisper` for audio), then ingest the `.txt`. Use a sidecar `<path>.m3meta.json` with `{"original_path": "..."}` so search results cite the original.

---

## 4. Search what you ingested

Just ask:

> search my files for "how does the embedder fallback work?"

The agent returns the matching paragraphs with their source file and section heading — hybrid keyword + semantic search, no setup. For a giant corpus, ask the agent to "list file summaries first" so you can triage before drilling in.

---

## 5. Backfilling old conversations (optional)

If you had conversations before installing M3, ingest them in one shot per format. The cursor (`memory\.chatlog_ingest_cursor.json`) tracks what's already in so re-running is safe.

```powershell
# Claude Code
python bin\chatlog_ingest.py --format claude-code `
    "$env:APPDATA\Claude\projects\<project-hash>\*.jsonl"

# Gemini CLI
python bin\chatlog_ingest.py --format gemini-cli `
    "$env:USERPROFILE\.gemini\tmp\*\logs.json"

# OpenCode (uses the Claude Code JSONL shape)
python bin\chatlog_ingest.py --format claude-code `
    "$env:APPDATA\opencode\**\*.jsonl"
```

---

## You're done

- **New conversations**: auto-captured (Claude / Gemini hooks).
- **Old conversations**: one `chatlog_ingest.py` call per client.
- **Directories**: `files_ingest` when you want fresh indexing.
- **Stale-file watcher**: `python -m files_memory.tools watch --directory C:\Users\you\Documents`.

### Windows-specific notes

- The watch daemon survives reboots via Task Scheduler. Create a task that runs `python -m files_memory.tools watch ...` at log on; set "Restart on failure" to keep it sticky.
- If `pip install --user` puts `m3.exe` somewhere unreachable, add the Scripts directory to your user PATH — see [install_windows.md § Common gotchas](install_windows.md#common-gotchas).
- The embedder uses CUDA on NVIDIA GPUs if a recent CUDA toolkit is on PATH; falls back to CPU otherwise. Vulkan / DirectML builds are documented in [EMBED_DEPLOYMENT.md](EMBED_DEPLOYMENT.md).
- PowerShell 7+ recommended; `winget install Microsoft.PowerShell` to upgrade.

Need more? [Full install reference](install_windows.md) · [Files-memory tool reference](tools/files_memory.md) · [Chat-log reference](CHATLOG.md) · [All 96 MCP tools](MCP_TOOLS.md)
