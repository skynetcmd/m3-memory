---
tool: bin/split_chatlog_from_core.py
sha1: 775fdaf7f742
mtime_utc: 2026-07-03T01:59:49.318036+00:00
generated_utc: 2026-07-03T20:00:03.921161+00:00
private: false
---

# bin/split_chatlog_from_core.py

## Purpose

split_chatlog_from_core — move chat_log rows out of the CORE memory DB into
the dedicated CHATLOG DB, making the two stores functionally separate.

Use this when a prior "integrated" configuration (CHATLOG_DB_PATH pointed at the
main memory DB) has accumulated `type='chat_log'` rows inside agent_memory.db and
you now want them in agent_chatlog.db, with the core store holding memory only.

The copy is done by EXPLICIT SHARED-COLUMN INTERSECTION, not `SELECT *`: the core
schema typically carries enrichment/belief/KG columns (source_group_id, pinned,
belief_alpha, vector_kind, ...) that the leaner chatlog schema neither has nor
needs. Copying positionally would corrupt the insert; copying the shared columns
drops the enrichment-only fields, which is correct for a chatlog store.

Order of operations (only under --commit):
  copy memory_items  (shared cols, INSERT OR IGNORE — idempotent)
  copy memory_embeddings for those rows (shared cols)
  rebuild target FTS
  VERIFY target row count >= source count   ← delete is skipped on mismatch
  delete chat_log rows (+ their embeddings) from source
VACUUM of the source is intentionally left to the operator (slow, locks the DB).

USAGE
=====

    # Dry run — print the plan and counts, write nothing (default).
    python bin/split_chatlog_from_core.py

    # Execute the move (backs nothing up for you — take backups first).
    python bin/split_chatlog_from_core.py --commit

    # Explicit paths (override all env/default resolution).
    python bin/split_chatlog_from_core.py         --source /path/to/agent_memory.db         --target /path/to/agent_chatlog.db --commit

DB SELECTION
============

Source (CORE, where chat_log rows currently live), in priority order:
  1. --source <path>
  2. $M3_DATABASE env var
  3. <engine_root>/agent_memory.db   (via m3_core.paths.resolve_engine_file)

Target (CHATLOG, where they should go), in priority order:
  1. --target <path>
  2. $M3_CHATLOG_DB_PATH / $CHATLOG_DB_PATH / legacy $CHATLOG_DB env var
  3. <engine_root>/agent_chatlog.db  (via m3_core.paths.resolve_engine_file)

The target must already carry the chatlog schema (memory_items,
memory_embeddings, memory_items_fts). A fresh engine root created by the
installer/homecoming already does; if yours does not, bootstrap it with the
chatlog migrations before running this.

SAFETY
======

Refuses to run if --source and --target resolve to the same file (that would be
a no-op "integrated" layout, not a split). The delete step is gated on a
post-copy count check and is skipped — leaving source intact — on any shortfall.
Take a filesystem backup of both DBs before --commit; this script does not.

---

## Entry points

- `def main()` (line 119)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--source` | CORE memory DB (default: $M3_DATABASE or engine agent_memory.db) | — |  | str |  |
| `--target` | CHATLOG DB (default: $CHATLOG_DB_PATH or engine agent_chatlog.db) | — |  | str |  |
| `--commit` | actually copy + delete (default: dry-run) | `False` |  | store_true |  |

---

## Environment variables read

- `CHATLOG_DB`
- `M3_DATABASE`

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (getenv_compat)`
- `m3_sdk (resolve_engine_file)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `p`` (line 101)
- `sqlite3.connect()  → `source`` (line 149)


---

## Notable external imports

- `m3_core.paths (resolve_engine_file)`

---

## File dependencies (repo paths referenced)

- `agent_chatlog.db`
- `agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
