---
tool: bin/cli_kb_browse.py
sha1: 113649874500
mtime_utc: 2026-04-07T04:04:41.324855+00:00
generated_utc: 2026-04-17T04:17:01.685581+00:00
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

- `def main()` (line 198)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `-n`, `--limit` | Max entries to show | — |  | int |  |
| `-t`, `--type` | Filter by type (fact, decision, knowledge, project…) | — |  | str |  |
| `-s`, `--search` | Search title/content (case-insensitive) | — |  | str |  |
| `--no-pager` | Print all without paging | — |  | store_true |  |
| `--db` | Override DB path | — |  | str |  |

## Environment variables read

- `ANSICON`
- `FORCE_COLOR`
- `WT_SESSION`

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (resolve_venv_python)`

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `str(db_path)`` (line 91)


## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

- `agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
