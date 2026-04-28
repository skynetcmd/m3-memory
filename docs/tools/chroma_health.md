---
tool: bin/chroma_health.py
sha1: 9899881f7960
mtime_utc: 2026-04-26T10:21:38.719170+00:00
generated_utc: 2026-04-28T15:48:17.257584+00:00
private: false
---

# bin/chroma_health.py

## Purpose

CLI script to report ChromaDB sync health metrics.

Provides visibility into the ChromaDB bi-directional sync system by querying
local SQLite tables and pinging the remote ChromaDB instance. Read-only; safe
for cron.

Usage:
    python bin/chroma_health.py                    # human-readable summary
    python bin/chroma_health.py --json             # JSON output
    python bin/chroma_health.py --check            # exit 0 (ok), 1 (warn), 2 (critical)
    python bin/chroma_health.py --quiet            # suppress info; show problems only

Can be wired into:
    - sync_all.py (call at end of sync to log health)
    - Windows Scheduled Task / cron job
    - Manual ad-hoc invocation

## Entry points

- `def main()` (line 349)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--json` | Output JSON instead of human-readable text | `False` |  | store_true |  |
| `--check` | Exit with status code: 0=ok, 1=warn, 2=critical (for cron alerting) | `False` |  | store_true |  |
| `--quiet` | Suppress info output; only show problems | `False` |  | store_true |  |
| `--database` | f'Path to SQLite DB (default: {DEFAULT_DB_PATH})' | `M3_DATABASE` |  | str |  |

## Environment variables read

- `CHROMA_BASE_URL`
- `M3_CHROMA_SYNC_QUEUE_MAX`
- `M3_CHROMA_SYNC_QUEUE_WARN`
- `M3_DATABASE`

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 179)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 62)


## Notable external imports

- `httpx`

## File dependencies (repo paths referenced)

- `agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
