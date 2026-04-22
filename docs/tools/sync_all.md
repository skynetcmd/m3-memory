---
tool: bin/sync_all.py
sha1: 1a8d9897929b
mtime_utc: 2026-04-21T20:43:55.949210+00:00
generated_utc: 2026-04-21T21:26:01.963752+00:00
private: false
---

# bin/sync_all.py

## Purpose

sync_all.py — Hourly sync runner (SQLite <-> PostgreSQL + ChromaDB).
Runs both pg_sync.py and chroma_sync, offline-tolerant.
Safe to call on any platform; skips gracefully if target unreachable.

Usage:
    python bin/sync_all.py
    python bin/sync_all.py --dry-run   (connectivity check only)

## Entry points

- `def main()` (line 112)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--dry-run` | Check connectivity only | `False` | Checks SYNC_TARGET_IP reachability, then calls pg_sync.py and chroma_sync_cli.py (both write to DBs/ChromaDB). | store_true | Checks reachability only; logs planned sync but skips subprocess calls (no actual writes). |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes all DB reads/writes against PATH for this run. |

## Environment variables read

- `POSTGRES_SERVER`
- `SYNC_TARGET_IP`

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg)`

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `[str(PY), str(BASE / 'bin' / 'chroma_sync_cli.py'), 'both']`` (line 91)
- `subprocess.run()  → `[str(PY), str(BASE / 'bin' / 'pg_sync.py')]`` (line 61)


## Notable external imports

- `platform`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
