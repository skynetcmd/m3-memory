---
tool: bin/m3_chatlog_enrich_backfill.py
sha1: 0942a0b25225
mtime_utc: 2026-05-04T22:04:47.605731+00:00
generated_utc: 2026-05-04T22:24:29.228448+00:00
private: false
---

# bin/m3_chatlog_enrich_backfill.py

## Purpose

Backfill `observation_queue` from existing chatlog rows.

Scans `memory_items WHERE type='chat_log'` in the chatlog DB, groups by
conversation_id, and INSERTs one row per conversation into `observation_queue`
of the main DB. Order is reverse-chronological (newest conversations first),
so when the periodic drain runs it processes the most recent material first.

Idempotent: uses `INSERT OR IGNORE`, so re-running won't duplicate enqueued
work for conversations still in the queue. Conversations already drained out
of the queue WILL be re-enqueued — use `--skip-already-enriched` to also
exclude conversations that already produced observations under the configured
target variant.

Cross-platform: only depends on Python stdlib + sqlite3.

---

## Entry points

- `def main()` (line 94)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--main-db` | f'Main DB path (default: {DEFAULT_MAIN_DB})' | `str(DEFAULT_MAIN_DB)` |  | str |  |
| `--chatlog-db` | f'Chatlog DB path (default: {DEFAULT_CHATLOG_DB})' | `str(DEFAULT_CHATLOG_DB)` |  | str |  |
| `--since` | Only enqueue conversations with rows since this ISO timestamp (e.g. 2026-04-01). | None |  | str |  |
| `--skip-already-enriched` | Skip conversation_ids already present under this variant in memory_items. | None |  | str |  |
| `--limit` | Cap how many conversations to enqueue. | None |  | int |  |
| `--dry-run` | Count what would be enqueued, write nothing. | `False` |  | store_true |  |
| `--yes` | Skip the confirmation prompt. | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `f'file:{chatlog_db}?mode=ro'`` (line 50)
- `sqlite3.connect()  → `f'file:{main_db}?mode=ro'`` (line 82)
- `sqlite3.connect()  → `str(main_db)`` (line 161)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `agent_chatlog.db`
- `agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
