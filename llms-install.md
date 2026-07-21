# <a href="README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> m3-memory — install guide for LLM agents (Cline)

This file tells an AI agent (e.g. Cline) exactly how to install and register
**m3-memory** as an MCP server, with no human in the loop. Follow the steps for
the user's OS in order. Every step is idempotent — safe to re-run.

m3-memory ships as a single PyPI package (`m3-memory`). It exposes an MCP server
over **stdio** plus a `m3` CLI. There is no cloud service, no API key, and no
account — everything runs locally.

---

## Step 0 — Detect the platform

- **Windows** → use PowerShell and `pip`.
- **macOS / Linux** → use a POSIX shell and `pipx` (the system Python is usually
  PEP 668 "externally managed", so `pip install` into it fails; `pipx` isolates
  the install).

If you cannot tell, ask the user which OS they are on before proceeding.

---

## Step 1 — Ensure prerequisites (Python ≥ 3.11)

Check `python --version` (or `python3 --version`). If it is missing or < 3.11:

- **Windows:** `winget install Python.Python.3.12`
- **macOS:** `brew install python@3.12 pipx git` then `pipx ensurepath`
- **Debian/Ubuntu/Fedora:** `sudo apt install -y pipx python3-venv git`
  (or `sudo dnf install -y pipx python3-virtualenv git`), then `pipx ensurepath`

`git` and `sqlite3` are optional (git speeds up payload fetch; the Python stdlib
`sqlite3` module is always present regardless of the CLI).

---

## Step 2 — Install the package

**Windows (PowerShell):**
```powershell
pip install m3-memory
```

**macOS / Linux:**
```bash
pipx install m3-memory
```

Verify it landed:
```bash
m3 --version
```
If `m3` is not on PATH after a `pipx` install, run `pipx ensurepath` and restart
the shell (or use `python -m m3_memory.cli` in place of `m3` for the rest of this
guide).

---

## Step 3 — Run the non-interactive setup

This fetches the system payload, brings up the sovereign local embedder
(BGE-M3 on `127.0.0.1:8082`, CPU-only, no GPU/Ollama/LM Studio required), and
initializes the databases. Run it non-interactively so no prompt blocks you:

```bash
m3 setup --non-interactive --capture-mode both
```

Defaults chosen by `--non-interactive`: SQLite backend (zero-infrastructure),
sovereign CPU embedder, chatlog capture on. No sudo is invoked by this step.

---

## Step 4 — Register the MCP server with Cline

**Preferred: let m3 write the entry.** `m3 setup` (run in Step 3) auto-detects
Cline and writes the MCP entry into Cline's `cline_mcp_settings.json` for you —
with the correct absolute Python interpreter, the bridge script path, and the
decoupled-root `env` block. This is the drift-free path; hand-authoring the entry
can point Cline at the wrong interpreter or omit the root env and cause the server
to fail on startup.

m3 only wires Cline if the extension's settings dir already exists, so if you ran
`m3 setup` **before** the user first launched the Cline extension, re-run it now
to pick Cline up:

```bash
m3 setup --non-interactive --capture-mode both
```

Then confirm the entry landed (Step 5 covers verification).

**Manual fallback (only if `m3 setup` did not wire it).** Merge an entry into the
`mcpServers` object of Cline's settings file (create the file as
`{"mcpServers": {}}` first if it does not exist; do **not** overwrite existing
servers). The file is:

- **Windows:** `%APPDATA%\Code\User\globalStorage\saoudrizwan.claude-dev\settings\cline_mcp_settings.json`
- **macOS:** `~/Library/Application Support/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`
- **Linux:** `~/.config/Code/User/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json`

> If the user runs VS Code Insiders or a VS Code fork, replace `Code` with the
> appropriate app-data folder (`Code - Insiders`, `VSCodium`, etc.).

The entry launches the m3 MCP **bridge** over stdio with an explicit interpreter.
Resolve the two machine-specific paths first:

```bash
# Bridge path — m3 doctor prints it on the "resolved bridge:" line:
m3 doctor            # look for "[OK] resolved bridge: <path>"
# Interpreter to launch it with (use as "command"):
python -c "import sys; print(sys.executable)"
```

If you prefer to read the bridge path programmatically instead of parsing doctor
output:

```bash
python -c "from m3_memory.installer import find_bridge; print(find_bridge())"
```

Use the interpreter as `command`, and the resolved bridge path as the single
`args` element. For example (paths will differ per machine — substitute the
values the commands above print):

```json
{
  "mcpServers": {
    "memory": {
      "command": "/absolute/path/to/python",
      "args": ["/absolute/path/to/bin/memory_bridge.py"],
      "env": {
        "M3_ENGINE_ROOT": "/home/you/.m3/engine",
        "M3_CONFIG_ROOT": "/home/you/.m3/config"
      },
      "disabled": false,
      "autoApprove": []
    }
  }
}
```

> Note: the server key is `memory` (m3's canonical MCP name), the `command` is a
> concrete Python interpreter, and `args` is the bridge script path — **not**
> `m3 serve` (that subcommand is the streamable-HTTP endpoint for claude.ai
> connectors, a different transport Cline does not use). Always prefer the
> `m3 setup` path above so these values are filled in correctly for you.

---

## Step 5 — Verify

```bash
m3 doctor
```

Expected: package + payload version, chatlog DB path with a captured-row count,
and the sovereign embedder service reporting healthy on `127.0.0.1:8082`. If
anything is stale or dead, run `m3 doctor --fix` (non-destructive: it repoints
dead config paths and de-duplicates MCP registrations), then re-run `m3 doctor`.

In Cline, reload the MCP servers (or restart VS Code). The `m3` server should
appear connected with its tool catalog available. Only a small essentials set
(~18 tools) loads at startup to keep context small; the rest of the 100+ tools
load on demand per domain — ask "load the files tools" and Cline pulls that
domain in.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `error: externally-managed-environment` on `pip install` | Use `pipx install m3-memory` instead (macOS/Linux). |
| `m3: command not found` after pipx | `pipx ensurepath`, restart shell; or use `python -m m3_memory.cli`. |
| Embedder not healthy in `m3 doctor` | `m3 embedder install` then re-run doctor. |
| Cline shows m3 disconnected | Confirm the `command` in Step 4 resolves for the VS Code process; prefer the `python -m m3_memory.cli` form. |
| Stale config paths after an upgrade | `m3 doctor --fix`. |

No API keys, accounts, or network egress are required for core operation. m3 is
local-first by design.
