# M3 Memory — Linux Quick Start

Get persistent memory + directory ingestion running on Linux in under five minutes. Works with Claude Code, Gemini CLI, OpenCode, and OpenClaw.

---

## 1. Install

**One-liner** (handles prerequisites + install + setup in one shot):

```bash
curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh | bash
```

The script auto-detects your distro, pulls Python/pipx/sqlite if missing, installs m3, then runs `m3 setup`.

**Already have Python/pipx?** Skip the script and install m3 directly:

```bash
pipx install m3-memory
m3 setup
```

> **PEP 668 error (`externally-managed-environment`)?** Your system Python is
> managed by the OS package manager. Install pipx via the package manager
> instead of pip:
> ```bash
> # Debian/Ubuntu/Mint
> sudo apt install pipx python3-venv
>
> # Fedora/RHEL/Rocky
> sudo dnf install pipx
>
> # Arch/Manjaro
> sudo pacman -S python-pipx
>
> # Alpine
> sudo apk add pipx
> ```
> Then re-run `pipx install m3-memory`.
>
> **No sudo?** If you're in the `sudo` group but have no tty (e.g. running
> over SSH inside `curl | bash`), open a second shell and run the package
> manager command there as root, then continue in your original shell.
> If you have no sudo access at all, ask a sysadmin to install `pipx` for
> you, or use a virtualenv:
> ```bash
> python3 -m venv ~/.venv/m3 && source ~/.venv/m3/bin/activate
> pip install m3-memory
> m3 setup
> ```

---

`m3 setup` detects every agent on PATH, asks a handful of questions, and drives the rest end-to-end:

- system payload
- embedder (everything's bundled — no LM Studio, no Ollama, no internet, no GPU required)
- per-agent MCP wiring (Claude Code, Gemini CLI, OpenCode, OpenClaw)
- chatlog Stop + PreCompact hooks
- final `m3 doctor` verification

Restart your agent and you're done. The rest of this doc covers the features.

> **Have a GPU?** The wizard asks once whether to add GPU acceleration on top of the default embedder for ~10-50× faster embeddings. You can also add it later with `m3 embedder install-gpu`.

> **Tool catalog stays small in your context.** m3 ships 87 MCP tools but groups them into 8 domains (memory, chatlog, files, entity, agent, tasks, conversations, admin). Only ~6 essentials load at MCP startup (~2,400 tokens vs ~16,100 if all 87 loaded eagerly). The agent pulls in a domain on demand — just say "load the files tools" and it does. Set `M3_TOOLS_LAZY=0` to disable.

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

```bash
claude mcp add memory m3
```

### Gemini CLI

`m3 setup` already wrote the entry into `~/.gemini/settings.json`. After install, just restart Gemini. That's it!

### OpenCode

Add to `opencode.json` (project root) or `~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "memory": { "type": "local", "command": ["m3"], "enabled": true }
  }
}
```

### OpenClaw

OpenClaw can't speak MCP natively. Run the bundled proxy on `localhost:9000` and point OpenClaw's OpenAI endpoint there:

```bash
bash bin/start_mcp_proxy.sh --background
# Then set OpenClaw's base URL to:  http://localhost:9000/v1
```

---

## 3. Embedder (Tier-2 service — optional but recommended)

The **Tier-1 in-process GGUF embedder** is active from the moment m3 starts — no extra steps. The **Tier-2 embed server** (port 8082) improves cold-start performance but is optional. M3 works fully without it.

### Install the binary first

```bash
m3 embedder install-gpu   # downloads the prebuilt wheel — no Rust needed for CPU
```

This installs the `m3-embed-server` binary via a prebuilt PyPI wheel. Despite the name, it works on CPU-only machines.

### Register as a systemd user service (recommended if systemd --user is available)

```bash
m3 embedder install       # registers + starts the systemd --user unit
```

> **systemd --user not available?** This fails on containers, SSH sessions
> without a D-Bus user session (`Failed to connect to user scope bus`), and
> minimal images. Use the nohup path below instead.

### Run directly (containers, SSH sessions, no systemd)

```bash
M3_EMBED_GGUF=~/bge-m3-GGUF-Q4_K_M.gguf \
    nohup m3-embed-server > ~/.m3/engine/embed-server.log 2>&1 &
```

To start automatically on boot without systemd:

```bash
crontab -e
# Add this line:
@reboot M3_EMBED_GGUF=~/bge-m3-GGUF-Q4_K_M.gguf m3-embed-server >> ~/.m3/engine/embed-server.log 2>&1 &
```

### Keep the systemd service alive across logout (headless / server)

If `loginctl` is available:

```bash
loginctl enable-linger "$USER"
```

Without this, a `systemd --user` service stops when your last session exits. Not needed with the nohup/cron approach.

### Verify

```bash
m3 doctor   # shows Tier-1 / Tier-2 status and embed roundtrip latency
```

---

## 4. Ingest a directory

Just tell your agent:

> ingest files at ~/Documents/notes

That's it. The agent indexes every supported file under the path. You can also scope it ("...as the `notes` corpus") or ask for fact extraction ("...and extract facts inline").

### Supported file types

m3 chunks these formats natively:

| Filetype | Extensions | How it's chunked |
|---|---|---|
| Markdown / RST | `.md` `.markdown` `.mdx` `.rst` | By heading tree |
| PDF | `.pdf` | One leaf per page |
| Plain text | `.txt` `.log` | Semantic paragraph |
| Code | `.py` `.ts` `.js` `.rs` `.go` `.java` `.rb` `.php` `.c` `.h` `.cpp` `.cs` `.swift` `.kt` `.scala` `.sh` `.sql` | Paragraph fallback |
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

If you had conversations before installing M3, ingest them in one shot per format. The cursor (`memory/.chatlog_ingest_cursor.json`) tracks what's already in so re-running is safe.

```bash
# Claude Code
python3 bin/chatlog_ingest.py --format claude-code \
    ~/.config/claude/projects/<project-hash>/*.jsonl

# Gemini CLI
python3 bin/chatlog_ingest.py --format gemini-cli \
    ~/.gemini/tmp/*/logs.json

# OpenCode (uses the Claude Code JSONL shape)
python3 bin/chatlog_ingest.py --format claude-code \
    ~/.local/share/opencode/**/*.jsonl
```

---

## You're done

- **New conversations**: auto-captured (Claude / Gemini hooks).
- **Old conversations**: one `chatlog_ingest.py` call per client.
- **Directories**: `files_ingest` when you want fresh indexing.
- **Stale-file watcher**: `python -m files_memory.tools watch --directory ~/Documents`.

Need more? [Full install reference](install_linux.md) · [Files-memory tool reference](tools/files_memory.md) · [Chat-log reference](CHATLOG.md) · [All 96 MCP tools](MCP_TOOLS.md)
