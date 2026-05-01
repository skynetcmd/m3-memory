# Good First Issues

New to M3 Memory? These are great starting points. Each issue is self-contained, well-scoped, and comes with existing scripts or tests to guide you.

Open an issue using the [bug report](.github/ISSUE_TEMPLATE/bug_report.md) or [feature request](.github/ISSUE_TEMPLATE/feature_request.md) template and mention which item below you'd like to work on.

---

## Beginner (no deep internals required)

### 1. Add `--dry-run` flag to `fix_bugs.py`
`fix_bugs.py` currently applies fixes automatically. Add a `--dry-run` flag that prints what would change without modifying anything.
- File: `fix_bugs.py`
- Skills: Python argparse, basic file I/O

### 2. Add `--dry-run` flag to `fix_db.py`
Same as above for the database repair utility.
- File: `fix_db.py`
- Skills: Python argparse, SQLite

### 3. Improve error messages in `validate_env.py`
When a dependency is missing or the LLM server is unreachable, the error messages are terse. Add suggestions (e.g., "Try: ollama serve" or "Check LM_ENDPOINTS_CSV in .env").
- File: `validate_env.py`
- Skills: Python, UX

### 4. Add `--json` output flag to `bench_memory.py`
The benchmark script prints results as plain text. Add a `--json` flag to emit structured JSON for CI integration.
- File: `bin/bench_memory.py`
- Skills: Python, JSON

---

## Intermediate (some internals knowledge helpful)

### 5. Add `memory_search` result count to `memory_cost_report`
`memory_cost_report()` tracks embed calls, tokens, writes. Add search call count tracking.
- Files: `bin/memory_bridge.py`, `bin/memory_core.py`
- Skills: Python, SQLite

### 6. Add `--agent` filter to `memory_export`
`memory_export` currently exports all memories. Add an `--agent` CLI flag to filter by `agent_id`.
- File: `bin/memory_bridge.py`
- Skills: Python, MCP tool schema

### 7. Write a `docker-compose.yml` for the full stack
Create a `docker-compose.yml` that spins up PostgreSQL + ChromaDB alongside M3 Memory for easy local federation setup.
- Skills: Docker, docker-compose, networking

### 8. Add Windows Task Scheduler instructions to install guide
`install_windows_homelab.md` mentions automated sync but doesn't show how to set up Task Scheduler for `bin/deep_sync.py`. Add step-by-step instructions with screenshots.
- File: `install_windows_homelab.md`
- Skills: Windows, documentation

---

## How to contribute

See [CONTRIBUTING.md](./CONTRIBUTING.md) for full setup instructions, test commands, and PR guidelines.
