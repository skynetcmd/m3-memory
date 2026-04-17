---
tool: bin/sync_all.py
sha1: eb959b693c79
mtime_utc: 2026-04-07T04:04:47.489006+00:00
generated_utc: 2026-04-17T04:17:01.767818+00:00
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
| `--dry-run` | Check connectivity only | — |  | store_true |  |

## Environment variables read

- `POSTGRES_SERVER`
- `SYNC_TARGET_IP`

## Calls INTO this repo (intra-repo imports)

_(none detected)_

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
