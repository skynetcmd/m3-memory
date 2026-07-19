---
tool: bin/migrate_warehouse_to_schema.py
sha1: bf305dae0a9b
mtime_utc: 2026-07-19T06:18:23.183734+00:00
generated_utc: 2026-07-19T19:29:22.730052+00:00
private: false
---

# bin/migrate_warehouse_to_schema.py

## Purpose

migrate_warehouse_to_schema.py — consolidate a PostgreSQL warehouse's tables
from the legacy ``public`` schema into the canonical ``m3_warehouse`` schema.

Older m3 warehouses stored the synced tables in ``public``. The canonical layout
(pg_warehouse_chatlog_v1.sql) puts them under ``m3_warehouse`` (unified core +
chat-log, with a type='chat_log' index). This tool moves any table still in
``public`` into ``m3_warehouse`` so warehouse sync (which now targets
``m3_warehouse``) sees your history instead of starting empty.

Safe by construction:
  * DRY-RUN by default — prints the per-table plan, changes nothing.
  * Idempotent — re-runnable; a table already consolidated is a no-op.
  * No data loss — public data is copied (ON CONFLICT DO NOTHING) into
    m3_warehouse and VERIFIED (wh count >= public count) BEFORE the public copy
    is dropped, and only with --drop-public. Without it, public is left intact.

Runs ON THE WAREHOUSE. Needs a role with USAGE+CREATE on m3_warehouse and
USAGE+SELECT (and DROP, for --drop-public) on public — typically a superuser or
the warehouse owner.

Usage:
    python bin/migrate_warehouse_to_schema.py --dsn <warehouse_dsn> [--dry-run] [--drop-public] [--yes]
    # --dsn defaults to M3_CDW_PG_URL / PG_URL when omitted.

Exit codes: 0 = success (or clean dry-run), 1 = error / verification failed.

---

## Entry points

- `def main()` (line 161)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--dsn` | Warehouse DSN (else M3_CDW_PG_URL/PG_URL). | None |  | str |  |
| `--dry-run` | Print the plan; change nothing. | `False` |  | store_true |  |
| `--drop-public` | After a verified copy, DROP the public.<table> copy. | `False` |  | store_true |  |
| `--yes` | Skip the confirmation prompt. | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `psycopg`
- `psycopg2`

---

## File dependencies (repo paths referenced)

- `pg_warehouse_chatlog_v1.sql`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
