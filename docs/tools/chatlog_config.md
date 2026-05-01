---
tool: bin/chatlog_config.py
sha1: 808a94152132
mtime_utc: 2026-04-22T01:03:02.023999+00:00
generated_utc: 2026-05-01T13:05:26.722715+00:00
private: false
---

# bin/chatlog_config.py

## Purpose

chatlog_config.py — configuration resolver for the chat log subsystem.

The three-mode (integrated/separate/hybrid) system has been collapsed: there
is now only a single chatlog DB path. If it happens to equal the main memory
DB path, chat log rows live in the main store (equivalent to the old
"integrated" behavior). Otherwise they live in a dedicated file (equivalent
to "separate"), and promote operations ATTACH the main DB and copy rows
across (what used to be called "hybrid" is just copy=True, which is the
default).

Resolution order for the chatlog DB path:
    1. CHATLOG_DB_PATH env var (explicit chatlog-only override, highest priority)
    2. active_database() ContextVar (per-call override set by the MCP tool
       dispatcher when a caller passes `database` on a chatlog_* tool)
    3. M3_DATABASE env var (unified main DB — chatlog shares it)
    4. .chatlog_config.json db_path field
    5. Default: memory/agent_chatlog.db (separate file; historical default)

Consumers:
    bin/chatlog_core.py       - write queue, search, promote, cost report
    bin/chatlog_status.py     - observability summary
    bin/chatlog_init.py       - interactive setup
    bin/chatlog_ingest.py     - stdin → bulk write
    bin/migrate_memory.py     - multi-target migration runner
    bin/m3_sdk.py             - get_chatlog_conn()

Zero dependency on memory_core, memory_bridge, or mcp_tool_catalog. Safe to import
from any module in bin/ without creating cycles.

Deprecated env var:
    CHATLOG_MODE — ignored with a warning. The former "integrated" behavior
    is now achieved by setting CHATLOG_DB_PATH equal to the main DB (or by
    leaving both unset when a shared M3_DATABASE covers everything).

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `CHATLOG_DB_PATH`
- `CHATLOG_DB_POOL_SIZE`
- `CHATLOG_DB_POOL_TIMEOUT`
- `M3_DATABASE`

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (_active_db)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 343)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `.chatlog_config.json`
- `.chatlog_ingest_cursor.json`
- `.chatlog_state.json`
- `agent_chatlog.db`
- `agent_memory.db`
- `alt_chatlog.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
