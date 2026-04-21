# CLI Reference

This document lists every command-line entry point that touches a SQLite database and how each one selects its target DB.

## Universal `--database` flag

Every DB-aware script accepts a standardized `--database PATH` flag. Resolution order is:

1. `--database PATH` CLI flag
2. `M3_DATABASE` environment variable
3. Default: `memory/agent_memory.db`

The flag is wired through `bin/m3_sdk.add_database_arg(parser)` and every value is normalized via `resolve_db_path(explicit)` to an absolute path before use. Scripts that shell out to other scripts (e.g. `sync_all.py` → `pg_sync.py`) set `M3_DATABASE` in the environment so subprocesses inherit the override.

Use the flag to route to separate stores for different workloads:

```bash
# Default — hits memory/agent_memory.db
python bin/memory_doctor.py

# Scratch DB for testing
python bin/memory_doctor.py --database memory/scratch.db

# Isolated benchmark DB
python benchmarks/longmemeval/bench_longmemeval.py --database memory/bench_longmemeval.db

# Separate chatlog file (unchanged behavior — still honored via CHATLOG_DB_PATH)
CHATLOG_DB_PATH=memory/my_chatlog.db python bin/chatlog_ingest.py --format claude-code --transcript-path foo.jsonl
```

## DB-aware scripts

| Script | Purpose | Extra DB-related flags |
| --- | --- | --- |
| `bin/bench_memory.py` | Write/search/dedup micro-benchmarks | — |
| `bin/ai_mechanic.py` | DESTRUCTIVE schema repair | `--database` is **required** (no default); also requires `--force` |
| `bin/build_kg_variant.py` | Build KG-enriched variant from a source variant | Honors legacy `AGENT_DB` env var as an alias |
| `bin/chatlog_init.py` | Interactive chatlog setup | `--db-path PATH` sets the chatlog DB in the saved config |
| `bin/chatlog_ingest.py` | Ingest a transcript into the chatlog DB | `--db PATH` (deprecated, alias for `CHATLOG_DB_PATH`) |
| `bin/chatlog_embed_sweeper.py` | Lazy-embed unembedded chatlog rows | — |
| `bin/cli_kb_browse.py` | Paginated knowledge base browser | `--db PATH` (legacy alias for `--database`) |
| `bin/cli_knowledge.py` | Add/update/search/delete knowledge items | — |
| `bin/chroma_sync_cli.py` | Bi-directional ChromaDB sync | — |
| `bin/embed_agent_instructions.py` | Ingest AGENT_INSTRUCTIONS.md as memories | — |
| `bin/memory_doctor.py` | Run health checks + repair | — |
| `bin/migrate_memory.py` | Migration runner (schema up/down) | `--target {main,chatlog,all}` selects DB family |
| `bin/migrate_flat_memory.py` | Ingest flat-file legacy memory | — |
| `bin/mission_control.py` | Status dashboard | Uses default resolution only |
| `bin/re_embed_all.py` | Re-embed every active item | — |
| `bin/secret_rotator.py` | Rotate vault-stored secrets | — |
| `bin/setup_memory.py` | Bootstrap (venv + deps + migrations) | Reads `M3_DATABASE` or `--database PATH` positionally |
| `bin/setup_secret.py` | Add/list/delete vault keys | — |
| `bin/sync_all.py` | Hourly sync runner (shells out to pg_sync + chroma_sync) | Propagates `--database` to subprocesses via `M3_DATABASE` |
| `bin/weekly_auditor.py` | PDF weekly audit report | — |
| `benchmarks/longmemeval/bench_longmemeval.py` | LongMemEval harness | Sets `M3_DATABASE` early so all ingest/search routes to the benchmark DB |

## Chatlog-specific overrides

The chatlog subsystem has its own resolver (see `bin/chatlog_config.py`):

| Env | Role |
| --- | --- |
| `CHATLOG_DB_PATH` | Explicit chatlog-only path override, highest priority for chatlog reads/writes |
| `M3_DATABASE` | Shared main DB; chatlog follows it unless `CHATLOG_DB_PATH` overrides |
| `CHATLOG_MODE` | **Deprecated** — ignored with a one-time warning. The three-mode system (integrated/separate/hybrid) has collapsed into path equality: same file = integrated behavior, different file = separate behavior, promote semantics switch automatically. |

See [CHATLOG.md](CHATLOG.md) for the full chatlog architecture.

## Scripts that don't need the flag

Some scripts in `bin/` don't touch SQLite and intentionally don't accept `--database`:

- `embed_server.py`, `embed_server_gpu.py` — LM Studio proxy servers
- `install_schedules.py` — cron/launchd installer
- `generate_configs.py`, `gen_mcp_inventory.py`, `gen_tool_inventory.py` — generators that write docs
- `pg_setup.py` — PostgreSQL DDL runner (separate target)
- `news_fetcher.py`, `macbook_status_server.py` — external service wrappers
