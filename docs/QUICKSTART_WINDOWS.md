# M3 Memory — Windows Quick Start

Get persistent memory + directory ingestion running on Windows in under five minutes. Works with Claude Code, Gemini CLI, OpenCode, and OpenClaw.

> No bash needed. Everything here is PowerShell.

---

## 1. Install

**Prerequisites** — run once in an **elevated PowerShell** (right-click → Run as administrator):

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
winget install -e --id SQLite.SQLite
```

> **Microsoft Store Python?** The Store version (`python3.exe` stub) blocks
> some installs. Use the winget version above instead — it puts a real
> `python.exe` on PATH.

Then install m3 and run the setup wizard **as your normal user** (not elevated):

```powershell
# Recommended: pipx isolates m3 and auto-manages PATH
winget install -e --id Python.Launcher   # ensures py.exe is available
pip install --user pipx
pipx ensurepath
# Open a new terminal so PATH refreshes, then:
pipx install m3-memory
m3 setup
```

**Prefer plain pip?**

```powershell
pip install --user m3-memory
m3 setup
```

> **`m3` not found after `pip install --user`?** pip puts the script in
> `%APPDATA%\Python\Python312\Scripts\` (adjust for your Python version).
> Add that folder to your user PATH:
> ```powershell
> $scripts = "$env:APPDATA\Python\Python312\Scripts"
> [Environment]::SetEnvironmentVariable(
>     "PATH", "$env:PATH;$scripts", "User")
> # Then open a new terminal.
> ```
> Using pipx avoids this entirely — it handles PATH automatically.

> **Prefer a graphical setup?** Run `m3 setup --gui` for a window with the same
> questions (recommended defaults pre-selected), a live install log, and a
> color-coded **Verify with m3 doctor** step. See
> [install_windows.md → Graphical setup](install_windows.md#graphical-setup).
> It still needs the prerequisites + package above first.

`m3 setup` detects every agent on PATH, asks a handful of questions, and drives the rest end-to-end:

- system payload
- embedder (everything's bundled — no LM Studio, no Ollama, no internet, no GPU required)
- per-agent MCP wiring (Claude Code, Gemini CLI, OpenCode, OpenClaw)
- chatlog Stop + PreCompact hooks
- final brief `m3 doctor` health check (`--verbose` for full detail)

Restart your agent and you're done. The rest of this doc covers the features.

> **Have a GPU?** The wizard asks once whether to add GPU acceleration on top of the default embedder for ~10-50× faster embeddings (needs CUDA Toolkit + nvcc on PATH, or Vulkan SDK). You can also add it later with `m3 embedder install-gpu`.

> **Tool catalog stays small in your context.** m3 ships 100+ MCP tools but groups them into 8 domains (memory, chatlog, files, entity, agent, tasks, conversations, admin). Only ~6 essentials load at MCP startup (~2,400 tokens vs ~16,100 if all of them loaded eagerly). The agent pulls in a domain on demand — just say "load the files tools" and it does. Set `M3_TOOLS_LAZY=0` to disable.

---

## 2. Connect M3 to your agent

If you ran `m3 setup` (step 1), every agent it detected on PATH is **already wired**. Restart the agent and the m3 MCP server is there. Skip to step 3.

If you skipped the wizard, or you're adding an agent later, here's the manual recipe per agent:

### Claude Code (recommended: plugin)

```
/plugin marketplace add skynetcmd/m3-memory
/plugin install m3@skynetcmd
```

> **No GitHub SSH key?** The `owner/repo` shorthand uses SSH. If you get a
> "Premature close" or "ERR_STREAM_PREMATURE_CLOSE" error, use the HTTPS URL:
> ```
> /plugin marketplace add https://github.com/skynetcmd/m3-memory
> /plugin install m3@skynetcmd
> ```

Then `/plugin reload` (or restart Claude Code). The plugin auto-registers the MCP, wires the chatlog Stop + PreCompact hooks, and adds 15 `/m3:*` slash commands plus two curator subagents — confirm with `/m3:health`.

If you'd rather wire it by hand:

```powershell
claude mcp add --scope user memory m3
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

## 3. Embedder (Tier-2 service — optional but recommended)

The **Tier-1 in-process GGUF embedder** is active from the moment m3 starts — no extra steps. The **Tier-2 embed server** (port 8082, Windows Service) improves cold-start performance but is optional. M3 works fully without it.

### Install the binary first

```powershell
m3 embedder install-gpu   # installs the prebuilt wheel — no Rust needed for CPU
```

This installs the `m3-embed-server` binary via a prebuilt PyPI wheel. Despite the name, it works on CPU-only machines. On NVIDIA machines it autodetects CUDA; on others it falls back to CPU.

### Register as a Windows Service (requires elevation)

The embed server registers as a Windows Service, which needs Administrator rights:

```powershell
# Open an elevated PowerShell (right-click → Run as administrator), then:
m3 embedder install
```

Verify from any terminal:

```powershell
m3 doctor   # shows Tier-1 / Tier-2 status and embed roundtrip latency
```

### If you can't elevate (no admin rights)

Run the server directly in a background PowerShell job:

```powershell
$env:M3_EMBED_GGUF = "$env:USERPROFILE\.m3-memory\_assets\models\bge-m3-Q4_K_M.gguf"
Start-Process -WindowStyle Hidden -FilePath "m3-embed-server" `
    -RedirectStandardOutput "$env:TEMP\m3-embed.log" `
    -RedirectStandardError  "$env:TEMP\m3-embed.log"
```

To start automatically at login without elevation, create a Task Scheduler entry:

```powershell
$gguf    = "$env:USERPROFILE\.m3-memory\_assets\models\bge-m3-Q4_K_M.gguf"
$action  = New-ScheduledTaskAction `
               -Execute "powershell.exe" `
               -Argument "-WindowStyle Hidden -Command `"& { `$env:M3_EMBED_GGUF='$gguf'; m3-embed-server }`"" `
               -WorkingDirectory "$env:USERPROFILE"
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0
Register-ScheduledTask -TaskName "m3-embed-server" `
    -Action $action -Trigger $trigger -Settings $settings `
    -RunLevel Limited -Force
```

> **Tip:** Task Scheduler entries with `RunLevel Limited` don't need elevation
> and survive reboots. Use `Unregister-ScheduledTask -TaskName "m3-embed-server"`
> to remove it.

---

## 4. Ingest a directory

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

## 5. Search what you ingested

Just ask:

> search my files for "how does the embedder fallback work?"

The agent returns the matching paragraphs with their source file and section heading — hybrid keyword + semantic search, no setup. For a giant corpus, ask the agent to "list file summaries first" so you can triage before drilling in.

---

## 6. Backfilling old conversations (optional)

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

- **Embedder as a Windows Service** requires an elevated terminal. If you can't elevate, use the Task Scheduler approach in §3 above — no admin rights needed.
- **The watch daemon** survives reboots the same way. Replace `m3-embed-server` with `python -m files_memory.tools watch --directory $env:USERPROFILE\Documents` in the `Register-ScheduledTask` snippet above.
- **PATH after `pip install --user`** — covered in §1. Using `pipx` avoids the issue entirely.
- **GPU acceleration** — CUDA autodetected if `nvcc` is on PATH; Vulkan also supported. Run `m3 embedder install-gpu` after installing CUDA Toolkit. Vulkan / DirectML builds: [EMBED_DEPLOYMENT.md](EMBED_DEPLOYMENT.md).
- **PowerShell 7+** recommended: `winget install Microsoft.PowerShell`.

Need more? [Full install reference](install_windows.md) · [Files-memory tool reference](tools/files_memory.md) · [Chat-log reference](CHATLOG.md) · [All MCP tools](MCP_TOOLS.md)
