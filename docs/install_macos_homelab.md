# M3 Agentic Memory — Full Install Plan (macOS)

> **Minimum viable install** (no homelab): Steps 1-4 + 6-8.
> The memory system works fully local via SQLite without Postgres or ChromaDB.

---

## Prerequisites

| Requirement | Check |
|---|---|
| **macOS 13+** | `sw_vers` |
| **Python 3.11+** | `python --version` |
| **Git** | `git --version` |
| **Homebrew** *(recommended)* | `brew --version` |

> If Python is not installed, run `brew install python` or download from [python.org](https://www.python.org/downloads/).

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
python -m venv .venv
source .venv/bin/activate
```

Verify the venv is active — your prompt should show `(.venv)` and:

```bash
python --version      # Should print Python 3.11+
pip --version           # Should point to .venv/bin/pip
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
python install_os.py
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

## Step 7 — Generate MCP configs

```bash
python bin/generate_configs.py
```

This does three things:
1. Patches `config/claude-settings.json` and `config/gemini-settings.json` with the correct absolute paths for your machine
2. Sets the correct `python` command in all MCP server entries
3. Generates `.mcp.json` in the project root — Claude Code automatically loads MCP servers from this file

The following bridges are registered:

| Server name | Script |
|---|---|
| `memory` | `bin/memory_bridge.py` |

> **Note:** `.mcp.json` is gitignored because it contains machine-specific absolute paths. Re-run `generate_configs.py` after cloning on a new machine.

---

## Step 8 — Verify everything

```bash
bash bin/mcp_check.sh
python bin/test_memory_bridge.py
python run_tests.py
```

---

## Step 9 — Set up scheduled tasks for hourly sync *(optional)*

**Option A — Automated:**

```bash
python bin/install_schedules.py
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
python -c "import keyring; print(keyring.get_keyring())"
```

This should show `KeychainKeyring`.

### `fnm` not recognized after install

Add fnm to your shell profile:

```bash
echo 'eval "$(fnm env --use-on-cd)"' >> ~/.zshrc
source ~/.zshrc
```

### `security` command errors when storing keys

Ensure you are not running in a headless SSH session — macOS Keychain requires a GUI login session for first-time access. If prompted, unlock the login keychain.
