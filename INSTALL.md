# INSTALL

Platform-specific setup notes for `m3-memory`. The quick path is the same on
every OS:

```
pip install m3-memory     # or pipx install m3-memory
mcp-memory install-m3     # fetches the system payload, runs post-install
mcp-memory doctor         # verify
```

If that works, stop here. The rest of this file is the stuff that differs
between operating systems and the reasons behind it.

## OS matrix

| Capability | Windows 11 | macOS (Apple Silicon / Intel) | Debian 12 / Ubuntu 24.04 / Fedora 38+ (PEP 668) | Older Linux (no PEP 668) |
|---|---|---|---|---|
| `python` ≥ 3.11 | `winget install Python.Python.3.12` | ships, or `brew install python@3.12` | `sudo apt install python3 python3-venv` | distro `python3` |
| Install method | `pip install m3-memory` | `pipx install m3-memory` (brew python is PEP 668) | `pipx install m3-memory` **required** | `pip install m3-memory` ok |
| `pipx` bootstrap | — | `brew install pipx` | `sudo apt install pipx` / `sudo dnf install pipx` | `pip install --user pipx` |
| `sqlite3` CLI | `winget install SQLite.SQLite` or [sqlite.org/download](https://sqlite.org/download.html) | ships in `/usr/bin/sqlite3` | `sudo apt install sqlite3` / `sudo dnf install sqlite` | `sudo yum install sqlite` |
| Python stdlib sqlite | built-in | built-in | built-in | built-in |
| `git` (for install-m3) | `winget install Git.Git` | ships with Xcode CLT | `sudo apt install git` | distro `git` |
| npm-global PATH (if using Gemini CLI) | handled by Node installer | `~/.npm-global/bin` — added to `.zshrc` by npm | `~/.npm-global/bin` — `install-m3` appends to `~/.profile` for non-interactive shells | same as Debian |
| Gemini CLI auto-register | `install-m3` writes `%USERPROFILE%\.gemini\settings.json` | `install-m3` writes `~/.gemini/settings.json` | same | same |
| Claude Code hooks | `install-m3` expects `~/.claude/settings.json` | same | same | same |

## What `install-m3` does for you

Post-install phase runs once at the end of `mcp-memory install-m3`. Every
step is additive and idempotent — safe to re-run via `mcp-memory update`:

1. **Gemini CLI** — if `gemini` is on PATH (or at `~/.npm-global/bin/gemini`),
   writes a `memory` MCP entry to `~/.gemini/settings.json`. Skips if already
   registered. Does nothing if Gemini isn't installed.
2. **sqlite3 CLI check** — prints a per-OS install hint if `sqlite3` isn't on
   PATH. Advisory only; we don't invoke sudo.
3. **npm-global PATH** — on Linux / macOS, appends
   `export PATH="$HOME/.npm-global/bin:$PATH"` to `~/.profile` if that dir
   exists and the line isn't already there. Fixes `gemini` being missing from
   cron and non-login sshd shells. No-op on Windows.
4. **Interactive prompts** (TTY only):
   - LLM endpoint: LM Studio (:1234), Ollama (:11434), or probe both.
   - Chatlog capture hooks: both, PreCompact-only, Stop-only, or none.

All four are skippable:

```bash
mcp-memory install-m3 --non-interactive          # silent defaults
mcp-memory install-m3 --endpoint http://localhost:11434/v1
mcp-memory install-m3 --capture-mode precompact
```

## Why pipx on PEP 668 distros

Debian 12+, Ubuntu 24.04+, Fedora 38+, and recent Arch mark their system
Python as "externally managed" (PEP 668). A plain `pip install m3-memory`
into the system interpreter fails with:

```
error: externally-managed-environment
```

`pipx` isolates the install into a per-command venv and adds the script
shim to `~/.local/bin`. That keeps system Python untouched and makes
upgrades (`pipx upgrade m3-memory`) a one-liner.

On macOS the Homebrew Python is also PEP 668-managed, so `pipx` is the
clean path there too. System Python on macOS is even older; don't use it.

## Diagnosing a broken install

```bash
mcp-memory doctor
```

Reports:
- package + installed payload version / tag / path
- chatlog DB path + captured-row count + last-capture timestamp
- Claude Stop/PreCompact hook state (on/off)
- Gemini `memory` MCP registration state

`mcp-memory chatlog status` drills into the chatlog subsystem (queue depth,
spill files, per-agent capture timestamps).

`mcp-memory chatlog doctor` is the same but exits nonzero on any warning —
suitable for CI / health checks.

## Per-OS walkthroughs

These documents cover the from-scratch homelab install including optional
Postgres + ChromaDB + LM Studio wiring. They are **not** required for a
working local-only setup:

- [docs/install_windows-powershell.md](docs/install_windows-powershell.md)
- [docs/install_macos.md](docs/install_macos.md)
- [docs/install_linux.md](docs/install_linux.md)
