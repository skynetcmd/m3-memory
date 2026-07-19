---
tool: bin/sync_all.py
sha1: 5b90e0107b3a
mtime_utc: 2026-07-19T06:36:07.540590+00:00
generated_utc: 2026-07-19T19:29:22.929632+00:00
private: false
---

# bin/sync_all.py

## Purpose

sync_all.py — Hourly sync runner (SQLite <-> PostgreSQL).
Runs pg_sync.py once per configured DB. Offline-tolerant.
Safe to call on any platform; skips gracefully if target unreachable or DB absent.

Usage:
    python bin/sync_all.py
    python bin/sync_all.py --dry-run   (connectivity check only)

DB list:
    Repo default: `memory/agent_memory.db`. The agent_memory manifest sweeps
    both `main` and `chatlog` targets internally, so chatlog data gets synced
    in the same pass without listing it separately. Bench DBs and other
    custom databases are NOT auto-detected — set M3_SYNC_DBS to include them.

    Example self-host override:
        M3_SYNC_DBS=memory/agent_memory.db:../m3-memory-bench/data/agent_bench.db

---

## Entry points

- `def main()` (line 240)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--dry-run` | Check connectivity only | `False` | Checks SYNC_TARGET_IP reachability, then calls pg_sync.py (writes to the PostgreSQL warehouse). | store_true | Checks reachability only; logs planned sync but skips subprocess calls (no actual writes). |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes all DB reads/writes against PATH for this run. |

---

## Environment variables read

- `M3_CDW_PG_URL`
- `M3_DATABASE`
- `M3_PG_SYNC_TIMEOUT`
- `M3_SYNC_DBS`

---

## Calls INTO this repo (intra-repo imports)

- `_task_runtime (add_log_file_arg, setup_task_runtime)`
- `_task_runtime (no_window_kwargs)`
- `m3_sdk (M3Context)`
- `m3_sdk (add_database_arg)`
- `m3_sdk (getenv_compat)`
- `m3_sdk (resolve_db_path)`
- `pg_fdw_sync`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `[str(PY), str(BASE / 'bin' / 'pg_sync.py'), '--db', str(db_path)]`` (line 142)


---

## Notable external imports

- `memory.backends (active_backend)`

---

## File dependencies (repo paths referenced)

- `memory/agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
