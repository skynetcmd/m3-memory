# <a href="README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> INSTALL

Manual install path for `m3-memory`. Most users should just run the
[one-line installer from the README](README.md#-install) — this file
exists for users who want to know what the script does, audit it before
running it, or run the steps by hand.

## Audit before running

```bash
curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh -o install.sh
less install.sh                         # read it
bash install.sh                         # run it
```

The script is ~180 lines, no obfuscation, refuses to run as root, uses
your normal user's `pipx`, and only invokes `sudo` for OS package
installs (one `apt`/`dnf`/`brew` call). Re-runs are idempotent.

Flags:

```
--cognitive-loop                             enable the background autonomous loop (v2026.5.4.5)
--capture-mode {both|stop|precompact|none}   default: both
--endpoint URL                               pin LLM_ENDPOINTS_CSV
--skip-prereqs                               assume pipx/git/sqlite3 already present
--no-install-m3                              stop after pipx install (don't fetch payload)
```

---

## TL;DR — manual path per OS

### Debian 12+ / Ubuntu 24.04+ / Fedora 38+ (PEP 668 distros)

System packages first (one sudo command), then the Python install as your normal user:

```bash
# As an admin user, install system prerequisites:
sudo apt update && sudo apt install -y pipx python3-venv git sqlite3 curl
# (Fedora/RHEL: sudo dnf install -y pipx python3-virtualenv git sqlite curl)
# (Arch:        sudo pacman -S --needed python-pipx git sqlite curl)

# As your normal user (or the same user, no sudo from here on):
pipx ensurepath
exec $SHELL -l                        # pick up ~/.local/bin in PATH
pipx install m3-memory
m3 setup                              # one-command wizard — install + agent wiring + embedder
```

### macOS

```bash
brew install pipx git sqlite          # python3 ships; pipx isolates the install
pipx ensurepath
exec $SHELL -l
pipx install m3-memory
m3 setup
```

### Windows 11

```powershell
winget install Python.Python.3.12 Git.Git SQLite.SQLite
pip install m3-memory
m3 setup
```

### Older Linux (no PEP 668)

```bash
sudo yum install -y python3-pip git sqlite       # or apt on pre-Bookworm
pip install --user m3-memory
m3 setup
```

`m3 setup` is the recommended path. It detects your agents (Claude Code,
Cursor, Cline, Gemini CLI, OpenCode, Antigravity, OpenClaw, Hermes), asks a
few questions, then drives every
install step end-to-end: install-m3, sovereign CPU embedder, per-agent MCP
wiring, chatlog hooks, and a final `doctor` verification. Power users can
still run each step manually with `m3 install-m3`, `m3 embedder install`,
etc. — see `m3 --help`.

> **Tool catalog stays small in your context.** m3 ships 100+ MCP tools but
> groups them into 9 domains (memory, chatlog, files, entity, agent, tasks,
> conversations, diagnostics, admin). Only the ~18 essentials load at MCP
> startup (~3,540 tokens, ~1.8% of a 200K window; the full catalog loads on
> demand). The agent pulls in a domain on demand — just say "load the files
> tools" and it does. Set `M3_TOOLS_LAZY=0` to disable.

If the TL;DR worked, stop here. The rest of this file explains why and what
gets installed.

---

## Prerequisites — what needs admin (sudo) once

`m3-memory` itself ships as a single Python package via PyPI and never asks
for sudo. But on a minimal Linux install you'll be missing the OS-level tools
the installer relies on. Install these once with admin rights, then everything
afterward runs as your normal user:

| Tool | Why we need it | Install (Debian 13 example) |
|---|---|---|
| `python3` ≥ 3.11 | runtime | `sudo apt install python3` (usually preinstalled) |
| `pipx` | recommended installer for PEP 668 distros (Debian 12+, Ubuntu 24.04+, Fedora 38+, Arch) | `sudo apt install pipx` |
| `python3-venv` | dependency of pipx on Debian/Ubuntu | `sudo apt install python3-venv` |
| `git` | `m3 setup` clones the system payload from GitHub (falls back to tarball if missing, but git is faster) | `sudo apt install git` |
| `sqlite3` CLI | for ad-hoc DB inspection — Python's `sqlite3` stdlib still works without it | `sudo apt install sqlite3` |
| `curl` | not strictly required, but the troubleshooting docs assume it | `sudo apt install curl` |

**One-liner for Debian 13 / Ubuntu 24.04+:**

```bash
sudo apt update && sudo apt install -y pipx python3-venv git sqlite3 curl
```

If you also want Gemini CLI or Claude Code as an MCP client, add Node.js:

```bash
sudo apt install -y nodejs npm
```

Everything below this point runs as your normal user. No more sudo needed.

---

## OS matrix

| Capability | Windows 11 | macOS (Apple Silicon / Intel) | Debian 12 / Ubuntu 24.04 / Fedora 38+ (PEP 668) | Older Linux (no PEP 668) |
|---|---|---|---|---|
| `python` ≥ 3.11 | `winget install Python.Python.3.12` | ships, or `brew install python@3.12` | `sudo apt install python3 python3-venv` | distro `python3` |
| Install method | `pip install m3-memory` | `pipx install m3-memory` (brew python is PEP 668) | `pipx install m3-memory` **required** | `pip install m3-memory` ok |
| `pipx` bootstrap | — | `brew install pipx` | `sudo apt install pipx` / `sudo dnf install pipx` | `pip install --user pipx` |
| `sqlite3` CLI | `winget install SQLite.SQLite` or [sqlite.org/download](https://sqlite.org/download.html) | ships in `/usr/bin/sqlite3` | `sudo apt install sqlite3` / `sudo dnf install sqlite` | `sudo yum install sqlite` |
| Python stdlib sqlite | built-in | built-in | built-in | built-in |
| `git` (for `m3 setup`) | `winget install Git.Git` | ships with Xcode CLT | `sudo apt install git` | distro `git` |
| npm-global PATH (if using Gemini CLI) | handled by Node installer | `~/.npm-global/bin` — added to `.zshrc` by npm | `~/.npm-global/bin` — `m3 setup` appends to `~/.profile` for non-interactive shells | same as Debian |
| Gemini CLI auto-register | `m3 setup` writes `%USERPROFILE%\.gemini\settings.json` | `m3 setup` writes `~/.gemini/settings.json` | same | same |
| Claude Code hooks | `m3 setup` expects `~/.claude/settings.json` | same | same | same |

---

## What `m3 setup` does for you

`m3 setup` is the one-command wizard. It runs `m3 install-m3` to fetch the
system payload, then orchestrates every other install step. All steps are
additive and idempotent — safe to re-run via `m3 update` or `m3 setup`
again:

1. **System payload** — `m3 install-m3` clones (or downloads as a tarball)
   the repo into `~/.m3-memory/repo`, pinned to the wheel version.
2. **Sovereign embedder** — `m3 embedder install` brings up the BGE-M3
   CPU service on `127.0.0.1:8082` using the GGUF bundled at
   `_assets/models/bge-m3-Q4_K_M.gguf` (Git LFS). No LM Studio / Ollama /
   GPU required.
3. **Optional GPU embedder** — the wizard offers `m3 embedder install-gpu`
   if you want CUDA / Vulkan / Metal acceleration on top of the CPU
   baseline. Auto-detects toolchain.
4. **Per-agent MCP wiring** — writes `memory` MCP entries for any of
   Claude Code / Cursor / Cline / Gemini CLI / OpenCode / Antigravity /
   OpenClaw it detects on your system.
5. **Chatlog hooks** — installs the `PreCompact` (and optionally `Stop`)
   hooks in Claude Code, and the `SessionEnd` hook in Gemini CLI.
6. **sqlite3 CLI check** — prints a per-OS install hint if `sqlite3` isn't
   on PATH. Advisory only; we don't invoke sudo.
7. **npm-global PATH** — on Linux / macOS, appends
   `export PATH="$HOME/.npm-global/bin:$PATH"` to `~/.profile` if that dir
   exists and the line isn't already there. Fixes `gemini` being missing
   from cron and non-login sshd shells. No-op on Windows.
8. **Interactive prompts** (TTY only):
   - Primary database backend: SQLite (default, zero-infrastructure) or
     PostgreSQL. Choosing PostgreSQL sets `M3_DB_BACKEND=postgres` and needs
     a reachable server via `M3_PRIMARY_PG_URL`. Non-interactively:
     `m3 install-m3 --db-backend {sqlite,postgres}`.
   - LLM endpoint: probe local OpenAI-compatible servers (Ollama :11434
     etc.), or pin a custom URL. m3's own embedder is sovereign and runs
     on :8082; this endpoint is only for *generation* (enrichment, SLM
     passes).
   - Chatlog capture hooks: both, PreCompact-only, Stop-only, or none.

All prompts are skippable:

```bash
m3 setup --non-interactive --capture-mode both                       # silent defaults
m3 setup --non-interactive --endpoint http://localhost:11434/v1
m3 setup --non-interactive --capture-mode precompact --install-gpu-embedder
```

Power users can also run any single step directly: `m3 install-m3`,
`m3 embedder install`, `m3 chatlog init --apply-claude`, etc. — see
`m3 --help`.

---

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

---

## Diagnosing a broken install

```bash
m3 doctor
```

Reports:
- package + installed payload version / tag / path
- chatlog DB path + captured-row count + last-capture timestamp
- Claude Stop/PreCompact hook state (on/off)
- Gemini `memory` MCP registration state
- sovereign embedder service status

`m3 chatlog status` drills into the chatlog subsystem (queue depth,
spill files, per-agent capture timestamps).

`m3 chatlog doctor` is the same but exits nonzero on any warning —
suitable for CI / health checks.

---

## Gemini CLI gotchas

Gemini CLI 0.39+ refuses to run in a non-trusted directory. Headless and
automated invocations need one of:

```bash
gemini --skip-trust --prompt "..."                    # per-call opt-out
GEMINI_CLI_TRUST_WORKSPACE=true gemini --prompt "..." # env-var opt-out
```

This affects:
- **Cron / systemd / CI** — set `GEMINI_CLI_TRUST_WORKSPACE=true` in the unit's `Environment=` block.
- **Hooks invoking `gemini`** — pass `--skip-trust` in the command.

Interactive shells can trust the directory once via Gemini's TUI prompt and
the choice persists; only headless contexts need the explicit opt-out.

The `memory` MCP entry in `~/.gemini/settings.json` is written automatically
by `m3 setup` (when Gemini is on PATH). The `SessionEnd` chatlog hook is
written by `m3 chatlog init --apply-gemini` (or automatically by `m3 setup`
when you accept the default capture mode).

---

## Per-OS walkthroughs

Quick installers for each OS:

- [docs/install_windows.md](docs/install_windows.md) — PowerShell + winget
- [docs/install_macos.md](docs/install_macos.md) — `curl … | bash` via Homebrew
- [docs/install_linux.md](docs/install_linux.md) — `curl … | bash` (apt / dnf / pacman / zypper / apk)

Full homelab walkthroughs covering optional Postgres + external
generation endpoints (not required for a local-only setup):

- [docs/install_windows_homelab.md](docs/install_windows_homelab.md)
- [docs/install_macos_homelab.md](docs/install_macos_homelab.md)
- [docs/install_linux_homelab.md](docs/install_linux_homelab.md)
