# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/m3_logo_icon.png" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> CLI Reference

## The `m3` command

Everything a pip/pipx install needs is under the `m3` command (`mcp-memory` is
a backwards-compatible alias). Run `m3 <command> --help` for flags.

**Install, upgrade, health**

| Command | What it does |
| --- | --- |
| `m3 setup` | Interactive one-command setup: payload, embedder, agent wiring, chatlog hooks, final `m3 doctor` (`--gui` for a window) |
| `m3 install-m3` | Fetch the system payload into the M3 root (default `~/.m3-memory/repo`); `--db-backend postgres` for a PostgreSQL primary store |
| `m3 reinstall` | Wipe and reinstall the payload (alias for `install-m3 --force`) |
| `m3 upgrade` | Upgrade the package end to end with the right installer (pipx / pip / pip --user), then re-wire and verify; `--from-pypi` moves a pipx install off a local wheel onto PyPI |
| `m3 update` | Re-sync the payload for the installed version (not a package upgrade) |
| `m3 uninstall` | Remove the payload and its config file (your databases are kept) |
| `m3 stop` | Stop every running m3 DB writer (cognitive loop, embed server, dashboard, MCP) |
| `m3 status` | One-line health verdict |
| `m3 doctor` | Full diagnostics (`--verbose` for detail, `--fix --fix-hooks` to repair wiring) |

**Services and subsystems**

| Command | What it does |
| --- | --- |
| `m3 embedder` | Shared embed server: `install-gpu`, `install`, `start`, `stop`, `status`, `uninstall`, `fetch-model`, `shared` / `unshared`, `backfill`, `reembed` |
| `m3 chatlog` | Chatlog operations: `init`, `status`, `doctor`, `hook-path` |
| `m3 schedules` | Background scheduled tasks: `verify`, `repair`, `list`, `add`, `remove` |
| `m3 governor` | Inspect / migrate legacy scheduled tasks to the background governor |
| `m3 dashboard` | Start the local web dashboard (localhost only) |
| `m3 serve` | Run the bridge as a streamable-HTTP MCP server (for claude.ai connectors) |
| `m3 wiki` | Generate a browsable wiki from core memories + files corpus (`generate`, `compile`, `status`) |
| `m3 fips` | FIPS crypto: `install-wolfssl`, `status` |
| `m3 enrich-pending` / `m3 extract-pending` | Drain pending enrichment / entity extraction |

**Tool domains** — every MCP catalog tool, callable from the shell as
`m3 <domain> <tool>` (add `--dry-run` to validate, `--yes` to confirm a
destructive tool; `m3 <domain> --help` lists the tools):

| Command | Domain |
| --- | --- |
| `m3 memory` | Curated long-term memory: write / search / graph / dedup / retention |
| `m3 files` | Directory ingestion and hybrid search over files |
| `m3 chat` | Chatlog tools (`chatlog_search`, `chatlog_promote`, …) |
| `m3 tasks` | Task creation, assignment, tree, results |
| `m3 agent` | Multi-agent registration, heartbeat, presence |
| `m3 admin` | Notifications, enrichment, GDPR |
| `m3 conversations` | Conversation start / append / search / summarize |
| `m3 diagnostics` | Health probes (`embedder_status`, `memory_doctor`) |
| `m3 entity` | Knowledge-graph entities |

---

## `bin/` scripts and their target database

The rest of this document lists every command-line entry point that touches a SQLite database and how each one selects its target DB. Run these `bin/*.py` scripts from a source checkout (or the installed payload at `~/.m3-memory/repo/bin`).

## Universal `--database` flag

Every DB-aware script accepts a standardized `--database PATH` flag. Resolution order is:

1. `--database PATH` CLI flag
2. `M3_DATABASE` environment variable
3. Default: `<engine_root>/agent_memory.db` — i.e. `~/.m3/engine/agent_memory.db`
   (the engine root resolves `M3_ENGINE_ROOT` → `M3_MEMORY_ROOT/engine` → `~/.m3/engine`;
   see [ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md#roots--precedence-the-single-source-of-truth)).
   A legacy `memory/agent_memory.db` in a dev checkout is used only if it already
   holds data.

The flag is wired through `bin/m3_sdk.add_database_arg(parser)` and every value is normalized via `resolve_db_path(explicit)` to an absolute path before use. Scripts that shell out to other scripts (e.g. `sync_all.py` → `pg_sync.py`) set `M3_DATABASE` in the environment so subprocesses inherit the override.

Use the flag to route to separate stores for different workloads:

```bash
# Default — hits <engine_root>/agent_memory.db (~/.m3/engine/agent_memory.db)
python bin/memory_doctor.py

# Scratch DB for testing
python bin/memory_doctor.py --database memory/scratch.db

# Separate chatlog file (unchanged behavior — still honored via CHATLOG_DB_PATH)
CHATLOG_DB_PATH=memory/my_chatlog.db python bin/chatlog_ingest.py --format claude-code --transcript-path foo.jsonl
```

---

## Running tests against an isolated DB

The test suites (`test_memory_bridge.py`, `test_debug_agent.py`, `test_mcp_proxy.py`) read their `DB_PATH` via `resolve_db_path(None)` at import time, so they honor `M3_DATABASE` set in the environment.

Isolated test run:

```bash
# Seed a fresh schema-complete scratch DB
python bin/setup_test_db.py --database memory/_test.db --force

# Run any test suite against it
M3_DATABASE=memory/_test.db python bin/test_memory_bridge.py
```

`setup_test_db.py` applies every forward migration in `memory/migrations/` (skipping `.down.sql` rollbacks). Pass `--force` to wipe the target before seeding.

---

## DB-aware scripts

| Script | Purpose | Extra DB-related flags |
| --- | --- | --- |
| `bin/bench_memory.py` | Write/search/dedup micro-benchmarks | — |
| `bin/ai_mechanic.py` | DESTRUCTIVE schema repair | `--database` is **required** (no default); also requires `--force` |
| `bin/augment_memory.py` | Post-ingest augmentation: adjacent-turn linking + SLM-based title enrichment | `link-adjacent` / `enrich-titles` / `all` subcommands; entity-enrichment requires `M3_SLM_CLASSIFIER=1` ([SLM_INTENT.md](SLM_INTENT.md)) |
| `bin/build_kg_variant.py` | Build KG-enriched variant from a source variant | Honors legacy `AGENT_DB` env var as an alias |
| `bin/chatlog_init.py` | Interactive chatlog setup | `--db-path PATH` sets the chatlog DB in the saved config |
| `bin/chatlog_ingest.py` | Ingest a transcript into the chatlog DB | `--db PATH` (deprecated, alias for `CHATLOG_DB_PATH`) |
| `bin/chatlog_embed_sweeper.py` | Lazy-embed unembedded chatlog rows | — |
| `bin/cli_kb_browse.py` | Paginated knowledge base browser | `--db PATH` (legacy alias for `--database`) |
| `bin/cli_knowledge.py` | Add/update/search/delete knowledge items | — |
| `bin/embed_agent_instructions.py` | Ingest AGENT_INSTRUCTIONS.md as memories | — |
| `bin/memory_doctor.py` | Run health checks + repair | — |
| `bin/migrate_memory.py` | Migration runner (schema up/down) | `--target {main,chatlog,all}` selects DB family |
| `bin/migrate_flat_memory.py` | Ingest flat-file legacy memory | — |
| `bin/mission_control.py` | Status dashboard | Uses default resolution only |
| `bin/re_embed_all.py` | Re-embed every active item | — |
| `bin/secret_rotator.py` | Rotate vault-stored secrets | — |
| `bin/setup_memory.py` | Bootstrap (venv + deps + migrations) | Reads `M3_DATABASE` or `--database PATH` positionally |
| `bin/setup_secret.py` | Add/list/delete vault keys | — |
| `bin/setup_test_db.py` | Seed a scratch DB with the full schema (for test isolation) | `--force` wipes existing file before seeding |
| `bin/sync_all.py` | Hourly sync runner (shells out to pg_sync) | Propagates `--database` to subprocesses via `M3_DATABASE` |
| `bin/weekly_auditor.py` | PDF weekly audit report | — |

---

## Chatlog-specific overrides

The chatlog subsystem has its own resolver (see `bin/chatlog_config.py`):

| Env | Role |
| --- | --- |
| `CHATLOG_DB_PATH` | Explicit chatlog-only path override, highest priority for chatlog reads/writes |
| `M3_DATABASE` | Shared main DB; chatlog follows it unless `CHATLOG_DB_PATH` overrides |
| `CHATLOG_MODE` | **Deprecated** — ignored with a one-time warning. The three-mode system (integrated/separate/hybrid) has collapsed into path equality: same file = integrated behavior, different file = separate behavior, promote semantics switch automatically. |

See [CHATLOG.md](CHATLOG.md) for the full chatlog architecture.

---

## SLM intent classifier (dormant by default)

A separate env-gated subsystem controls the Small-Language-Model intent
classifier that `bin/augment_memory.py` uses and that future retrieval
wiring can consume:

| Env | Role |
| --- | --- |
| `M3_SLM_CLASSIFIER` | Master gate for `bin/slm_intent.py`. Off by default. |
| `M3_INTENT_ROUTING` | Separate gate for the retrieval-side consumer (role-boost + predecessor-pull in `memory_core`). On by default; set `0` to disable. |
| `M3_SLM_PROFILE` | Named profile to load; defaults to `default` (reads `config/slm/default.yaml`). |
| `M3_SLM_PROFILES_DIR` | `os.pathsep`-separated list of dirs searched before `config/slm/`. Bench harnesses stack their own dir here. |

Profiles are YAML, one file per name. See [SLM_INTENT.md](SLM_INTENT.md) for
the full format reference, the three useful gate combinations, and
walkthroughs for Ollama / LM Studio / OpenAI / bench-harness setups.

---

## Scripts that don't need the flag

Some scripts in `bin/` don't touch SQLite and intentionally don't accept `--database`:

- `embed_server.py`, `embed_server_gpu.py` — LM Studio proxy servers
- `install_schedules.py` — cron/launchd installer
- `generate_configs.py`, `gen_mcp_inventory.py`, `gen_tool_inventory.py` — generators that write docs
- `pg_setup.py` — PostgreSQL DDL runner (separate target)
- `news_fetcher.py`, `macbook_status_server.py` — external service wrappers
