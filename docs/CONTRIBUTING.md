# Contributing to M3 Memory

Thank you for your interest in contributing! M3 Memory is a local-first agentic memory layer for MCP agents. This guide covers how to get set up, run the tests, and submit changes.

---

## Getting Started

### 1. Fork and Clone

```bash
git clone https://github.com/skynetcmd/m3-memory.git
cd m3-memory
```

### 2. Set Up Your Environment

```bash
python -m venv .venv
source .venv/bin/activate          # macOS/Linux
# .\.venv\Scripts\Activate.ps1    # Windows PowerShell

pip install -r requirements.txt
```

### 3. Validate Your Setup

```bash
python validate_env.py
```

This checks that your Python version, dependencies, and (optionally) local LLM server are all properly configured.

---

## Running the Tests

```bash
# Full end-to-end test suite (requires a running local LLM server)
python run_tests.py

# Retrieval quality benchmarks (MRR, Hit@k, latency)
python bin/bench_memory.py

# Lint (Ruff)
ruff check bin/ memory/

# Type check (Mypy)
mypy bin/ --ignore-missing-imports
```

The test suite covers memory CRUD, hybrid search, contradiction detection, GDPR operations, knowledge graph traversal, bitemporal queries, and more.

---

## Project Layout

```
bin/                    Core MCP bridges and utility scripts
  memory_bridge.py      Main MCP server — all 44 memory tools (sourced from mcp_tool_catalog.py)
  llm_failover.py       LLM endpoint auto-selection
  auth_utils.py         AES-256 vault and OS keyring integration
  embedding_utils.py    Vector embedding helpers
memory/                 SQLite schema and migration scripts
config/                 Agent configuration templates (CLAUDE.md, GEMINI.md)
docs/                   Architecture diagrams, API reference, and OS install guides
scripts/                Maintenance utilities (fix_bugs.py, fix_db.py, fix_lint.py)
tests/                  End-to-end test suite
benchmarks/             Benchmark harnesses and result catalogs (not shipped on PyPI)
  longmemeval/          LongMemEval-S/M harness + README + RUN_CATALOG
  Phase1/               LoCoMo harness (in progress)
```

---

## Reproducing Benchmarks

Benchmark harnesses live under `benchmarks/<suite>/` and are **not part of the published PyPI package** — they require a repository checkout. Each suite directory has its own `README.md` with the exact commands, dataset setup, and result catalog.

```bash
# LongMemEval-S (stock config, 89.0% overall)
python benchmarks/longmemeval/bench_longmemeval.py

# See per-suite README for ablations, baselines, and environment variables
cat benchmarks/longmemeval/README.md
```

Harnesses depend on modules under `bin/` (retrieval, embeddings, LLM failover); the `sys.path` bootstrap at the top of each harness handles this automatically when run from the repo root.

---

## Submitting Changes

1. Create a branch: `git checkout -b feat/your-feature`
2. Make your changes and run the tests
3. Commit with a clear message describing the change
4. Open a pull request against `main`

Please keep PRs focused — one feature or fix per PR makes review faster.

---

## Community & Discussion

Join our Discord server for questions, design discussions, and contributor chat:

[![Discord](https://img.shields.io/badge/Join%20Discord-M3--Memory%20Community-5865F2?logo=discord&logoColor=white)](https://discord.gg/ZcJ3EGC99B)

- **#ask-anything** — setup help and how-to questions
- **#bug-reports** — report issues (include steps to reproduce + logs)
- **#memory-design** — architecture debates and new algorithm ideas
- **#search-quality** — search tuning, benchmarks, and retrieval improvements

**M3_Bot** is active in the server and can answer questions from the docs directly (`!ask <question>`).

---

## Reporting Issues

Open an issue on GitHub with:
- What you expected to happen
- What actually happened
- Your OS, Python version, and LLM server (e.g., Ollama 0.3, LM Studio 0.2.x)
- Relevant log output from `logs/`

---

## Code Style

- Python 3.11+
- Ruff for linting (`ruff check bin/ memory/`)
- Mypy for type checking (`mypy bin/ --ignore-missing-imports`)
- No external cloud APIs — all features must work fully offline

---

## Database hygiene

All M3 databases run in WAL (Write-Ahead Log) mode. The WAL file (`<db>-wal`)
and shared-memory file (`<db>-shm`) are **part of the live database** and must
never be deleted manually.

**Rules for contributors:**

- **Never delete `*-wal` or `*-shm` files.** If the file looks empty or
  stale, it may still hold unflushed writes. Deleting it silently reverts
  the database to the last checkpointed state.
- To legitimately shrink a large WAL, run:
  ```bash
  sqlite3 <db-path> 'PRAGMA wal_checkpoint(TRUNCATE);'
  ```
- All new database connections must call `apply_pragmas` from
  `bin/sqlite_pragmas.py` rather than setting inline `PRAGMA` statements.
  Use `profile_for_db(path)` to select the right profile automatically.
- Long-running write tools should call `PRAGMA wal_checkpoint(PASSIVE)`
  every ~1 000 rows or every 60 s, and `PRAGMA wal_checkpoint(TRUNCATE)`
  at clean exit to keep WAL size bounded.
- The `journal_size_limit` pragma in each profile is the hard ceiling on WAL
  file size. Do not lower it below 64 MiB (`production`/`chatlog`) or
  256 MiB (`bench`) without a documented rationale.

---

*M3 Memory: the industrial-strength foundation for agents that remember.*
