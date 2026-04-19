---
tool: bin/cli_kb_browse.py
sha1: 0d0310f324a4
mtime_utc: 2026-04-18T22:28:14.281644+00:00
generated_utc: 2026-04-19T00:39:15.972619+00:00
private: false
---

# bin/cli_kb_browse.py

## Purpose

cli_kb_browse.py — Browse knowledge base entries in rank (importance) order.
Cross-platform: macOS, Windows, Linux.

Usage:
    python bin/cli_kb_browse.py              # all entries, paged
    python bin/cli_kb_browse.py -n 20        # top 20
    python bin/cli_kb_browse.py -t fact      # filter by type
    python bin/cli_kb_browse.py -s proxmox   # search title/content
    python bin/cli_kb_browse.py --no-pager   # dump all, no paging

## Entry points

- `def main()` (line 199)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `-n`, `--limit` | Max entries to show | None | Returns all entries (no LIMIT clause in SQL), sorted by importance DESC | int | Limits result set to N rows using SQL LIMIT |
| `-t`, `--type` | Filter by type (fact, decision, knowledge, project…) | None | No type filtering; SQL WHERE clause omitted | str | Filters by type; supports wildcard (e.g., fact*) or exact match (e.g., "fact") |
| `-s`, `--search` | Search title/content (case-insensitive) | None | No search filtering; fetches all entries | str | Filters entries where title or content matches (case-insensitive LIKE); exact match if quoted |
| `--no-pager` | Print all without paging | `False` | Renders all entries in paged mode (50 lines/page, prompts to continue) | store_true | Prints all rendered entries to stdout without pagination |
| `--db` | Override DB path | None | Uses hardcoded DB_PATH (REPO_ROOT / "memory" / "agent_memory.db") | str | Connects to specified SQLite DB file instead of default |

## Environment variables read

- `ANSICON`
- `FORCE_COLOR`
- `WT_SESSION`

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (resolve_venv_python)`

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `str(db_path)`` (line 92)


## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

- `agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
