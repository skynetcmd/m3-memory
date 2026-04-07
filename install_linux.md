# M3 Agentic Memory — Full Install Plan (Linux)

> **Minimum viable install** (no homelab): Steps 1-4 + 6-7.
> The memory system works fully local via SQLite without Postgres or ChromaDB.

---

## Prerequisites

| Requirement | Check |
|---|---|
| **Ubuntu 22.04+ / Fedora 38+ / Debian 12+** | `cat /etc/os-release` |
| **Python 3.11+** | `python3 --version` or `python --version` |
| **Git** | `git --version` |
| **pip** | `pip3 --version` or `pip --version` |

> If Python is not installed:
> - **Ubuntu/Debian:** `sudo apt update && sudo apt install python3 python3-pip python3-venv git`
> - **Fedora:** `sudo dnf install python3 python3-pip git`

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

> **Note:** Use `python3` to create the venv (some Linux distros don't ship a `python` alias). Once the venv is activated, `python` is always available.

Verify the venv is active — your prompt should show `(.venv)` and:

```bash
python --version       # Should print Python 3.11+
pip --version           # Should point to .venv/bin/pip
```

---

## Step 3 — Install Python dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

> **Note on `psycopg2-binary`:** If the wheel fails, install the build dependencies first:
> - **Ubuntu/Debian:** `sudo apt install libpq-dev python3-dev`
> - **Fedora:** `sudo dnf install libpq-devel python3-devel`
>
> Or skip it entirely if you are not using PostgreSQL homelab sync.

---

## Step 4 — Run the automated installer

```bash
python install_os.py
```

This performs 6 actions automatically:

| # | Action | Linux behavior |
|---|--------|---------------|
| 1 | Installs Node.js version manager | Installs `fnm` via curl |
| 2 | Creates `memory/` and `logs/` directories | Standard `os.makedirs` |
| 3 | Creates/reuses `.venv` and installs requirements | Uses `.venv/bin/pip` |
| 4 | Runs `bin/migrate_memory.py` | Initializes the SQLite schema |
| 5 | Prompts for `AGENT_OS_MASTER_KEY` | Stores via Secret Service (GNOME Keyring / KWallet) |
| 6 | Attempts initial PostgreSQL sync | Safe to fail if no homelab |

---

## Step 5 — Store API keys in the system keyring

The `AGENT_OS_MASTER_KEY` is set during Step 4. For additional API keys, use Python's `keyring` library from inside the activated venv:

```bash
python -c "import keyring; keyring.set_password('system', 'GROK_API_KEY', 'YOUR-KEY')"
python -c "import keyring; keyring.set_password('system', 'PERPLEXITY_API_KEY', 'YOUR-KEY')"
```

> `AGENT_OS_MASTER_KEY` is **required** for the encrypted vault.
> Grok and Perplexity keys are only needed if you use those services.

To verify a key was stored:

```bash
python -c "import keyring; print(keyring.get_password('system', 'AGENT_OS_MASTER_KEY'))"
```

> **Keyring backend requirements:**
> - **GNOME (Ubuntu, Fedora GNOME):** `sudo apt install gnome-keyring` or `sudo dnf install gnome-keyring`. Requires an active D-Bus session.
> - **KDE:** KWallet is used automatically.
> - **Headless / SSH:** Install `keyrings.alt` for a file-based fallback: `pip install keyrings.alt`

---

## Step 6 — Set server addresses *(homelab only)*

```bash
export POSTGRES_SERVER="YOUR_SERVER_IP"
export CHROMA_BASE_URL="http://YOUR_SERVER_IP:8000"
```

For persistence across sessions, add these lines to `~/.bashrc` (or `~/.zshrc` if using Zsh):

```bash
echo 'export POSTGRES_SERVER="YOUR_SERVER_IP"' >> ~/.bashrc
echo 'export CHROMA_BASE_URL="http://YOUR_SERVER_IP:8000"' >> ~/.bashrc
source ~/.bashrc
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
| `custom_pc_tool` | `bin/custom_tool_bridge.py` |
| `memory` | `bin/memory_bridge.py` |
| `grok_intel` | `bin/grok_bridge.py` |
| `web_research` | `bin/web_research_bridge.py` |
| `debug_agent` | `bin/debug_agent_bridge.py` |

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

Install the PostgreSQL development headers:

```bash
# Ubuntu/Debian
sudo apt install libpq-dev python3-dev

# Fedora
sudo dnf install libpq-devel python3-devel
```

Or skip it entirely if you are not using PostgreSQL homelab sync.

### `keyring` cannot find a backend

Linux requires an active Secret Service provider. Check which backend is active:

```bash
python -c "import keyring; print(keyring.get_keyring())"
```

- If it shows `PlaintextKeyring` or `FailKeyring`, install a proper backend:
  - **GNOME:** `sudo apt install gnome-keyring` and ensure D-Bus is running
  - **KDE:** KWallet should work automatically
  - **Headless / CI:** `pip install keyrings.alt` for an encrypted file-based keyring

### `fnm` not recognized after install

Add fnm to your shell profile:

```bash
echo 'eval "$(fnm env --use-on-cd)"' >> ~/.bashrc
source ~/.bashrc
```

### `python3-venv` missing (Ubuntu/Debian)

Ubuntu ships Python without `venv` by default:

```bash
sudo apt install python3-venv
```

### Permission denied on `bin/` scripts

```bash
chmod +x bin/*.sh bin/*.py
```
