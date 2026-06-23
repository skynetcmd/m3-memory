# M3 Agentic Memory — Full Install Plan (Windows / PowerShell)

> **Looking for the standard install?** This document covers the advanced
> repo-clone / homelab path (Postgres sync, ChromaDB, scheduled tasks).
> For a normal install see [install_windows.md](install_windows.md) and
> [QUICKSTART_WINDOWS.md](QUICKSTART_WINDOWS.md).

> **Minimum viable install** (no homelab): Steps 1-4 + 6-8.
> The memory system works fully local via SQLite without Postgres or ChromaDB.

---

## Prerequisites

| Requirement | Check |
|---|---|
| **Windows 10/11** | `winver` |
| **Python 3.11+** | `python --version` |
| **Git** | `git --version` |
| **PowerShell 5.1+** (ships with Windows) | `$PSVersionTable.PSVersion` |

> If Python is not installed, run `winget install -e --id Python.Python.3.12`
> in an elevated PowerShell. **Avoid the Microsoft Store Python** — it installs
> a stub launcher that blocks some installs. The winget version puts a real
> `python.exe` on PATH.

---

## Step 1 — Clone the repository

Skip this if you already ran the clone.

```powershell
git clone https://github.com/skynetcmd/m3-memory.git
cd m3-memory
```

---

## Step 2 — Create and activate the virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

> **ExecutionPolicy error?** Run this once in an elevated (Admin) PowerShell:
> ```powershell
> Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
> ```
> Then retry the `Activate.ps1` command.

Verify the venv is active — your prompt should show `(.venv)` and:

```powershell
python --version      # Should print Python 3.11+
pip --version         # Should point to .venv\Scripts\pip.exe
```

---

## Step 3 — Install Python dependencies

```powershell
pip install --upgrade pip
pip install -r requirements.txt
```

> **Note on `psycopg2-binary`:** This should install cleanly on Windows via wheel. If it fails, you can skip it — it is only needed for homelab PostgreSQL sync and is not required for local-only operation.

> **Note on `pywin32`:** This is listed as a Windows-only dependency in `requirements.txt` and will install automatically. It is required for the Windows Credential Manager integration used by the `keyring` library.

---

## Step 4 — Run the automated installer

```powershell
python install_os.py
```

This performs 6 actions automatically:

| # | Action | Windows behavior |
|---|--------|-----------------|
| 1 | Installs Node.js version manager | Installs `nvm-windows` via `winget` (or prompts manual install) |
| 2 | Creates `memory/` and `logs/` directories | Standard `os.makedirs` |
| 3 | Creates/reuses `.venv` and installs requirements | Uses `.venv\Scripts\pip.exe` |
| 4 | Runs `bin/migrate_memory.py` | Initializes the SQLite schema |
| 5 | Prompts for `AGENT_OS_MASTER_KEY` | Stores in Windows Credential Manager via `keyring` |
| 6 | Attempts initial PostgreSQL sync | Safe to fail if no homelab |

> If `winget` is unavailable for the nvm-windows install, download the latest release manually from [nvm-windows releases](https://github.com/coreybutler/nvm-windows/releases). **Restart your terminal after installing nvm-windows.**

---

## Step 5 — Store API keys in Windows Credential Manager

The `AGENT_OS_MASTER_KEY` is set during Step 4. For additional API keys, use Python's `keyring` library from inside the activated venv:

```powershell
python -c "import keyring; keyring.set_password('system', 'GROK_API_KEY', 'YOUR-KEY')"
python -c "import keyring; keyring.set_password('system', 'PERPLEXITY_API_KEY', 'YOUR-KEY')"
```

> `AGENT_OS_MASTER_KEY` is **required** for the encrypted vault.
> Grok and Perplexity keys are only needed if you use those services.

To verify a key was stored:

```powershell
python -c "import keyring; print(keyring.get_password('system', 'AGENT_OS_MASTER_KEY'))"
```

---

## Step 6 — Set server addresses *(homelab only)*

```powershell
$env:POSTGRES_SERVER = "YOUR_SERVER_IP"
$env:CHROMA_BASE_URL = "http://YOUR_SERVER_IP:8000"
```

For persistence across sessions, add these to your PowerShell profile:

```powershell
# Open your profile in a text editor:
notepad $PROFILE

# Add these lines and save:
$env:POSTGRES_SERVER = "YOUR_SERVER_IP"
$env:CHROMA_BASE_URL = "http://YOUR_SERVER_IP:8000"
```

> If `$PROFILE` does not exist yet:
> ```powershell
> New-Item -Path $PROFILE -ItemType File -Force
> ```

**Skip this step if running fully local.**

---

## Step 7 — Wire MCP clients

> **Modern path (recommended):** `m3 setup` (or `m3 install-m3`) handles
> MCP wiring automatically. Use that unless you need the legacy config files.

**Quick wiring:**

```powershell
# Claude Code
claude mcp add --scope user memory m3

# Gemini CLI
m3 chatlog init --apply-gemini
```

**Legacy path** (generates machine-specific config files for older setups):

```powershell
python bin\generate_configs.py
```

This patches `config/claude-settings.json` and `config/gemini-settings.json`
with absolute paths and generates `.mcp.json` in the project root.
`.mcp.json` is gitignored — re-run after cloning on a new machine.

---

## Step 8 — Verify everything

```powershell
m3 doctor                            # canonical health check
python bin\test_memory_bridge.py
python run_tests.py
# With Git Bash or WSL only:
bash bin/mcp_check.sh
```

---

## Step 9 — Set up scheduled tasks for hourly sync *(optional)*

**Option A — Automated:**

Run from an **elevated (Admin) PowerShell**:

```powershell
python bin/install_schedules.py
```

This creates four Windows Task Scheduler entries using `schtasks`:

| Task name | Schedule | Purpose |
|---|---|---|
| `AgentOS_HourlySync` | Every hour | Bi-directional PostgreSQL sync |
| `AgentOS_Maintenance` | Daily at 03:00 | Memory maintenance |
| `AgentOS_WeeklyAuditor` | Fridays at 16:00 | Weekly audit |
| `AgentOS_SecretRotator` | Monthly on the 1st at 02:00 | Secret rotation |

**Option B — Manual (single sync task):**

```powershell
$pythonExe = "$PWD\.venv\Scripts\python.exe"
$syncScript = "$PWD\bin\pg_sync.py"

schtasks /Create /TN "AgentOS_HourlySync" `
    /TR "cmd /c `"$pythonExe`" `"$syncScript`" >> `"$PWD\logs\sync.log`" 2>&1" `
    /SC HOURLY /ST 00:00 /F
```

To verify scheduled tasks were created:

```powershell
schtasks /Query /TN "AgentOS_HourlySync"
```

To delete a scheduled task:

```powershell
schtasks /Delete /TN "AgentOS_HourlySync" /F
```

---

## Troubleshooting

### `Activate.ps1` cannot be loaded — execution policy

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
```

### `pip install` fails on `psycopg2-binary`

This means the pre-built wheel is unavailable. Options:
1. Install `psycopg[binary]` instead (the newer async driver, also in `requirements.txt`)
2. Skip it entirely if you are not using PostgreSQL homelab sync

### `keyring` cannot find a backend

Ensure `pywin32` is installed (`pip install pywin32`). This provides the Windows Credential Manager backend. If issues persist:

```powershell
python -c "import keyring; print(keyring.get_keyring())"
```

This should show `WinVaultKeyring`. If it shows a different backend, run:

```powershell
pip install --force-reinstall keyring pywin32
```

### `nvm` not recognized after install

Restart your terminal. `nvm-windows` modifies the system PATH, which only takes effect in new sessions.

### `nvm` / `winget` gives "Permission denied" (not "command not found")

A system-wide binary exists but is not accessible to the current user. The
installer (as of v2026.6.4+) now handles this automatically and falls back
to a user-scoped install. If you hit this on an older install, re-run
`python install_os.py` after upgrading m3-memory.

### Python not on PATH / Microsoft Store Python

If `python` resolves to the Windows Store stub:
1. **Settings → Apps → App execution aliases** — toggle off both Python entries.
2. Re-run `winget install -e --id Python.Python.3.12` to install the real interpreter.
3. Open a new terminal and verify: `where.exe python` should point to
   something under `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`.

### Python not found / wrong version

Ensure Python is on your PATH:

```powershell
where.exe python
```

If it points to the Windows Store alias, disable it: **Settings > Apps > App execution aliases > Python** (toggle off).
