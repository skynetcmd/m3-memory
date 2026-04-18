---
tool: bin/cli_knowledge.py
sha1: 58608ef16ebb
mtime_utc: 2026-04-07T00:27:48.664201+00:00
generated_utc: 2026-04-18T05:16:53.100150+00:00
private: false
---

# bin/cli_knowledge.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

- `def main()` (line 22)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `-a`, `--add` | Add a knowledge item with this content | — |  | str |  |
| `-u`, `--update` | Update an existing knowledge item by ID | — |  | str |  |
| `-s`, `--search` | Search knowledge items | — |  | str |  |
| `-l`, `--list` | List recent knowledge items | — |  | store_true |  |
| `-d`, `--delete` | Delete a knowledge item by ID | — |  | str |  |
| `-c`, `--content` | Updated content for the item (with -u) | `` |  | str |  |
| `-t`, `--type` | Filter or set item type (use 'all' or '?' to list types in DB) | `` |  | str |  |
| `-k`, `--limit` | Number of results for search/list (default: 5) | `5` |  | int |  |
| `--title` | Title for added/updated item | `` |  | str |  |
| `--source` | Optional source for added item | `` |  | str |  |
| `--tags` | Comma-separated tags for added item | `` |  | str |  |
| `--metadata` | Raw JSON metadata string (overrides source/tags on add, appends/replaces on update) | `` |  | str |  |
| `--importance` | Importance score for update (0.0 to 1.0) | `-1.0` |  | float |  |
| `--reembed` | Force vector re-embedding during update | — |  | store_true |  |
| `--hard` | Permanently delete from database (requires exact string 'WIPE') | — |  | str |  |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (resolve_venv_python)`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

- `memory.knowledge_helpers (add_knowledge, search_knowledge, list_knowledge, delete_knowledge, get_all_types, update_knowledge)`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
