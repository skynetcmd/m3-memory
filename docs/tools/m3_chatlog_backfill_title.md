---
tool: bin/m3_chatlog_backfill_title.py
sha1: 30536db20b60
mtime_utc: 2026-04-28T02:32:27.080198+00:00
generated_utc: 2026-04-28T15:48:17.315120+00:00
private: false
---

# bin/m3_chatlog_backfill_title.py

## Purpose

m3_chatlog_backfill_title — Backfill missing/useless titles from content.

Free-win FTS5 lift from the 2026-04-26 chatlog analysis (memory id
37633aff). Title is part of the FTS index, so rows with title='user' or
title=NULL are effectively unsearchable by keyword. This tool replaces
useless titles with the first 100 chars of content.

Idempotent: rows that already have meaningful titles are left alone. The
"useless" set is configurable via --useless-titles.

Quick start:
    python bin/m3_chatlog_backfill_title.py --dry-run
    python bin/m3_chatlog_backfill_title.py

## Entry points

- `def main()` (line 224)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--core` |  | `False` |  | store_true |  |
| `--chatlog` |  | `False` |  | store_true |  |
| `--core-db` |  | None |  | str |  |
| `--chatlog-db` |  | None |  | str |  |
| `--useless-titles` | Comma-separated list of titles to treat as useless. Default: user,assistant,system,message,chat_log,None,'',etc. | None |  | str |  |
| `--min-chars` | Skip rows whose content is shorter than this. Default 10. | `10` |  | int |  |
| `--max-title-chars` | Cap derived titles at this many chars. Default 100. | `100` |  | int |  |
| `--limit` |  | None |  | int |  |
| `--dry-run` |  | `False` |  | store_true |  |
| `--skip-backup` |  | `False` |  | store_true |  |
| `--yes`, `-y` |  | `False` |  | store_true |  |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `f'file:{db_path}?mode=ro'`` (line 207)
- `sqlite3.connect()  → `f'file:{db_path}?mode=ro'`` (line 99)
- `sqlite3.connect()  → `str(db_path)`` (line 127)


## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

- `agent_chatlog.db`
- `agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
