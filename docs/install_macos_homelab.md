# M3 Agentic Memory — Full Install Plan (macOS)

> **Looking for the standard install?** This document covers the advanced
> repo-clone / homelab path (Postgres sync, ChromaDB, scheduled tasks).
> For a normal install use the one-liner:
> ```bash
> curl -fsSL https://raw.githubusercontent.com/skynetcmd/m3-memory/main/install.sh | bash
> ```
> See [install_macos.md](install_macos.md) and [QUICKSTART_MACOS.md](QUICKSTART_MACOS.md).

> **Minimum viable install** (no homelab): Steps 1-4 + 6-8.
> The memory system works fully local via SQLite without Postgres or ChromaDB.

---

## Prerequisites

| Requirement | Check |
|---|---|
| **macOS 13+** | `sw_vers` |
| **Python 3.11+** | `python3 --version` |
| **Git** | `git --version` |
| **Homebrew** *(recommended)* | `brew --version` |

> If Python is not installed: `brew install python@3.13`
> Avoid the macOS-shipped `/usr/bin/python3` — it is old and externally managed.

---

## Step 1 — Clone the repository

Skip this if you already ran the clone.

```bash
git clone https://github.com/skynetcmd/m3-memory.git
cd m3-memory
```

---

## Step 2 — Create and activate the virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Verify the venv is active — your prompt should show `(.venv)` and:

```bash
python --version      # Should print Python 3.11+
pip --version         # Should point to .venv/bin/pip
```

---

## Step 3 — Install Python dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

> `validate_env.py` passing does **not** mean dependencies are installed — run this regardless.

---

## Step 4 — Run the automated installer

```bash
python3 install_os.py
```

This performs 6 actions automatically:

| # | Action | macOS behavior |
|---|--------|---------------|
| 1 | Installs Node.js version manager | Installs `fnm` via curl |
| 2 | Creates `memory/` and `logs/` directories | Standard `os.makedirs` |
| 3 | Creates/reuses `.venv` and installs requirements | Uses `.venv/bin/pip` |
| 4 | Runs `bin/migrate_memory.py` | Initializes the SQLite schema |
| 5 | Prompts for `AGENT_OS_MASTER_KEY` | Stores in macOS Keychain via `keyring` |
| 6 | Attempts initial PostgreSQL sync | Safe to fail if no homelab |

---

## Step 5 — Store API keys in macOS Keychain

The `AGENT_OS_MASTER_KEY` is set during Step 4. For additional API keys, use the `security` command:

```bash
security add-generic-password -s "GROK_API_KEY" -a "$USER" -w "YOUR-KEY"
security add-generic-password -s "PERPLEXITY_API_KEY" -a "$USER" -w "YOUR-KEY"
```

> `AGENT_OS_MASTER_KEY` is **required** for the encrypted vault.
> Grok and Perplexity keys are only needed if you use those services.

To verify a key was stored:

```bash
security find-generic-password -s "AGENT_OS_MASTER_KEY" -w
```

---

## Step 6 — Set server addresses *(homelab only)*

```bash
export POSTGRES_SERVER="YOUR_SERVER_IP"
export CHROMA_BASE_URL="http://YOUR_SERVER_IP:8000"
```

For persistence across sessions, add these lines to `~/.zshrc`:

```bash
echo 'export POSTGRES_SERVER="YOUR_SERVER_IP"' >> ~/.zshrc
echo 'export CHROMA_BASE_URL="http://YOUR_SERVER_IP:8000"' >> ~/.zshrc
source ~/.zshrc
```

**Skip this step if running fully local.**

---

## Step 7 — Wire MCP clients

> **Modern path (recommended):** `m3 setup` (or `m3 install-m3`) handles
> MCP wiring automatically. Use that unless you need the legacy config files.

**Quick wiring:**

```bash
# Claude Code (--scope user writes to the user config, available in all projects)
claude mcp add --scope user memory m3

# Gemini CLI
m3 chatlog init --apply-gemini
```

**Legacy path** (generates machine-specific config files for older setups):

```bash
python3 bin/generate_configs.py
```

This patches `config/claude-settings.json` and `config/gemini-settings.json`
with absolute paths and generates `.mcp.json` in the project root.
`.mcp.json` is gitignored — re-run after cloning on a new machine.

---

## Step 8 — Verify everything

```bash
m3 doctor                        # canonical health check
bash bin/mcp_check.sh            # legacy MCP connectivity check (optional)
python3 bin/test_memory_bridge.py
python3 run_tests.py
```

---

## Step 9 — Set up scheduled tasks for hourly sync *(optional)*

**Option A — Automated:**

```bash
python3 bin/install_schedules.py
```

This installs crontab entries from `bin/crontab.template`.

**Option B — Manual (single sync task):**

```bash
crontab -e
# Add this line:
0 * * * * /path/to/m3-memory/bin/pg_sync.sh >> /path/to/m3-memory/logs/cron.log 2>&1
```

To verify the crontab was installed:

```bash
crontab -l
```

---

## Troubleshooting

### `psycopg2-binary` fails to install

Install PostgreSQL client libraries first:

```bash
brew install libpq
```

Or skip it entirely if you are not using PostgreSQL homelab sync.

### `keyring` cannot find a backend

macOS should use the Keychain backend automatically. Verify with:

```bash
python3 -c "import keyring; print(keyring.get_keyring())"
```

This should show `KeychainKeyring`. If it shows something else, reinstall:

```bash
pip install --force-reinstall keyring
```

### `fnm` not recognized after install

Add fnm to your shell profile:

```bash
echo 'eval "$(fnm env --use-on-cd)"' >> ~/.zshrc
source ~/.zshrc
```

### `fnm` gives "Permission denied" (not "command not found")

A system-wide `fnm` binary exists but is not executable by your user.
The installer (as of v2026.6.4+) handles this automatically and installs
fnm into your home directory. If you hit this on an older install, re-run
`python3 install_os.py` after upgrading m3-memory.

### `security` command errors when storing keys

macOS Keychain requires a GUI login session for first-time access — it
does not work over a headless SSH session without a forwarded Keychain agent.
Options:
- Run the setup locally (not over SSH) and let `keyring` store to Keychain.
- Over SSH: install `keyrings.alt` for a file-based fallback:
  `pip install keyrings.alt` — then re-run `python3 install_os.py`.
- Or store keys as environment variables in `~/.zshrc` instead of Keychain.
