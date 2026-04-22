# How to Upgrade m3-memory

This guide covers upgrading an existing checkout in place — pulling new code, updating dependencies, running schema migrations, and keeping the host OS current. For a fresh install see [`docs/install_linux.md`](install_linux.md), [`docs/install_macos.md`](install_macos.md), or [`docs/install_windows-powershell.md`](install_windows-powershell.md).

---

## TL;DR (safe default)

From the repo root, in order:

```bash
# 1. Back up the DB before you touch anything
python bin/migrate_memory.py backup --yes

# 2. Sync code
git fetch --all --prune
git pull --ff-only

# 3. Update deps inside your venv
source .venv/bin/activate           # Windows: .\.venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install --upgrade -r requirements.txt

# 4. Apply any pending DB migrations
python bin/migrate_memory.py status
python bin/migrate_memory.py up --yes

# 5. Verify
python -m pip_audit --strict
pytest -q
```

Each step is expanded below. If anything fails, see **[Rolling back](#rolling-back)**.

---

## 1. Back up first

Migrations are reversible by design, but DB corruption from a mid-flight crash is not. Always back up before `up` or `down`.

```bash
python bin/migrate_memory.py backup --yes
```

The backup uses SQLite's online-backup API (consistent snapshot even under concurrent writes) and lands in the backup directory saved in settings. Pass `--out /path/to/dir` to override.

You'll also see `.bak` files in `memory/` from the tooling's safety net — e.g., `agent_memory.db.pre-up-<timestamp>.bak`. Those are created automatically by `up`/`down`/`restore`; keep them until you've verified the upgrade.

---

## 2. Pull new code

```bash
git status                  # Confirm working tree is clean
git fetch --all --prune
git log HEAD..@{u} --oneline # Preview what's about to land
git pull --ff-only
```

If you have local work in progress, stash it first (`git stash push -m "pre-upgrade wip"`) and pop it after the upgrade completes.

If `--ff-only` refuses, your branch has diverged — rebase or merge deliberately; don't force it.

---

## 3. Update Python dependencies

### 3a. Project runtime (in the project venv)

```bash
# Linux / macOS
source .venv/bin/activate
# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1

pip install --upgrade pip
pip install --upgrade -r requirements.txt
```

This re-resolves against the pins in `requirements.txt` / `pyproject.toml`. Add `--upgrade-strategy eager` to also bump transitive deps (slower, more churn).

### 3b. Dev / lint / test extras (optional)

```bash
pip install --upgrade -e ".[dev]"
```

Pulls `pytest`, `ruff`, `mypy`, `twine`, etc. from the `[project.optional-dependencies].dev` list.

### 3c. Targeted CVE bumps

If `pip-audit` flags a package whose pin hasn't been bumped yet, upgrade it in place:

```bash
pip install --upgrade "cryptography>=46.0.7" "pytest>=9.0.3" \
                      "authlib>=1.6.11" "python-multipart>=0.0.26"
```

Transitive deps (pulled in by `fastmcp`, `mcp`, etc.) may lag their upstream — upgrading them directly in the venv is safe and catches CVEs ahead of the next upstream release.

### 3d. Audit after upgrading

```bash
python -m pip_audit --strict                    # scans the active environment
python -m pip_audit --requirement requirements.txt --strict  # scans declared deps
```

Expect `No known vulnerabilities found` in both. If not, follow 3c.

---

## 4. Apply database migrations

Migrations live in `memory/migrations/` as numbered `NNN_name.up.sql` / `NNN_name.down.sql` pairs (currently 001–018). `migrate_memory.py` tracks applied versions in the DB itself.

```bash
# Show current version and what's pending
python bin/migrate_memory.py status

# Preview the SQL for the next pending migration without applying
python bin/migrate_memory.py plan

# Apply everything pending (non-interactive)
python bin/migrate_memory.py up --yes

# Apply up to a specific version
python bin/migrate_memory.py up --to 17 --yes

# Dry-run (print what would happen, change nothing)
python bin/migrate_memory.py up --dry-run
```

> **Note:** When the MCP server starts, it runs `up --yes` automatically. If a migration silently isn't getting applied, check for a pre-existing backup collision in `memory/` or a lock from a stale process.

### If the migration fails mid-run

The script integrity-checks the restored DB and aborts loudly if it's not `ok`. You'll see a `pre-up-*.bak` alongside `agent_memory.db`. Restore with:

```bash
python bin/migrate_memory.py restore memory/agent_memory.db.pre-up-<timestamp>.bak --yes
```

---

## 5. Verify the upgrade

```bash
# Quick health check
python bin/memory_doctor.py

# Full test suite (fast; integration tests are skipped unless configured)
pytest -q

# Confirm MCP server starts and advertises the expected tool catalog
python bin/mcp_tool_catalog.py --check
```

If you use Postgres sync or ChromaDB, also run:

```bash
python bin/pg_setup.py --check
python bin/chroma_sync_cli.py status
```

---

## 6. Keep the host OS and system tools current

m3-memory itself doesn't require the OS to be fresh, but the Python toolchain, OpenSSL, and Git underneath it do affect security and compatibility.

### Linux

```bash
# Debian / Ubuntu
sudo apt update && sudo apt upgrade -y
sudo apt install --only-upgrade python3 python3-pip python3-venv git openssl ca-certificates

# Fedora / RHEL
sudo dnf upgrade --refresh -y
sudo dnf install python3 python3-pip git openssl ca-certificates

# Arch
sudo pacman -Syu
```

Reboot if the kernel or glibc was upgraded.

### macOS

```bash
# System updates
softwareupdate --install --all

# Homebrew + formulas
brew update
brew upgrade
brew upgrade python@3.11 git openssl@3   # explicit bump if needed
brew cleanup -s
```

If you use `pyenv` or `asdf`, bump the Python version the venv was built against and recreate the venv (`rm -rf .venv && python3 -m venv .venv`).

### Windows (PowerShell as Administrator)

```powershell
# Windows updates
Install-Module PSWindowsUpdate -Force -Scope CurrentUser   # first time only
Get-WindowsUpdate -Install -AcceptAll

# winget-managed tools
winget upgrade --all --include-unknown

# Python / Git via winget
winget upgrade Python.Python.3.11
winget upgrade Git.Git
```

After a Python minor-version bump on any platform, recreate the venv:

```bash
deactivate
rm -rf .venv                # Windows: Remove-Item -Recurse -Force .venv
python3 -m venv .venv
source .venv/bin/activate   # Windows: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

---

## 7. Optional components

### Embedding server / local LLM runtime

- **LM Studio** — update from inside the app (Settings → Check for updates).
- **Ollama** — `curl -fsSL https://ollama.com/install.sh | sh` (Linux/macOS) or re-run the installer on Windows.
- **llama.cpp / vLLM** — `git pull && pip install -e .` inside their checkout.

After upgrading the model runtime, restart `bin/embed_server.py` so it picks up new CUDA/Metal support.

### Postgres data warehouse (optional)

```bash
# Debian / Ubuntu
sudo apt upgrade postgresql postgresql-contrib
sudo systemctl restart postgresql

# Then reapply any PG-side schema changes
python bin/pg_setup.py
python bin/pg_sync.py --once
```

### ChromaDB (optional, if running a local Chroma server)

```bash
pip install --upgrade chromadb-client
# If you run the server too:
pip install --upgrade chromadb
```

Then:

```bash
python bin/chroma_sync_cli.py reindex
```

---

## Rolling back

If the upgrade left things in a worse state:

### Roll back the DB

```bash
python bin/migrate_memory.py down --to <previous_version> --yes
# or restore from the pre-upgrade backup
python bin/migrate_memory.py restore memory/agent_memory.db.pre-up-<timestamp>.bak --yes
```

### Roll back the code

```bash
git log --oneline -20           # find the known-good commit
git checkout <sha>              # detached HEAD for testing
# or, if you pulled into main:
git reset --hard <sha>          # destructive; be sure
```

### Roll back the venv

```bash
deactivate
rm -rf .venv                    # Windows: Remove-Item -Recurse -Force .venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Troubleshooting

- **`pip install --upgrade` with no args** → that's a pip usage error. Pass package names or `-r requirements.txt`.
- **`Defaulting to user installation because normal site-packages is not writeable`** → you're running system pip, not the venv's pip. Activate the venv first.
- **`migrate_memory.py` hangs** → another process holds the SQLite lock. Find it with `lsof memory/agent_memory.db` (Linux/macOS) or Resource Monitor (Windows); stop it, then retry.
- **MCP server won't start after upgrade** → check `memory/logs/` for the last traceback, and confirm `python bin/migrate_memory.py status` shows no pending migrations.
- **Pytest failures on first run after upgrade** → run `pytest --lf -vv` to re-run only the failing tests with full output; most often an env var (`M3_*`) or local LLM endpoint regressed.

More recipes in [`docs/TROUBLESHOOTING.md`](TROUBLESHOOTING.md).
