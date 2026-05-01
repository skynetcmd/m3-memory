---
tool: bin/cli_knowledge.py
sha1: a90253ea9ab4
mtime_utc: 2026-04-21T20:44:20.430808+00:00
generated_utc: 2026-05-01T13:05:26.772517+00:00
private: false
---

# bin/cli_knowledge.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `def main()` (line 32)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `-a`, `--add` | Add a knowledge item with this content | — | No item created; prompts help if no action specified | str | Creates new knowledge item with specified content using add_knowledge() |
| `-u`, `--update` | Update an existing knowledge item by ID | — | No update occurs; prompts help if no action specified | str | Updates item by ID using update_knowledge(); requires -c/--content or other modifiers |
| `-s`, `--search` | Search knowledge items | — | No search; prompts help if no action specified | str | Searches KB using search_knowledge() with specified query; limited by -k |
| `-l`, `--list` | List recent knowledge items | `False` | No listing; prompts help if no action specified | store_true | Lists recent items using list_knowledge() up to -k limit |
| `-d`, `--delete` | Delete a knowledge item by ID | — | No deletion; prompts help if no action specified | str | Soft-deletes item by ID (hard delete if --hard WIPE is passed) |
| `-c`, `--content` | Updated content for the item (with -u) | `` | Ignored unless -u is specified | str | Sets new content for updated item; appended to update_knowledge() call |
| `-t`, `--type` | Filter or set item type (use 'all' or '?' to list types in DB) | `` | No type filtering; no type set on add. With no action, lists all types if provided. | str | On add: sets item type (default "knowledge" if empty); on search/list: filters by type (wildcard support) |
| `-k`, `--limit` | Number of results for search/list (default: 5) | `5` | search_knowledge() and list_knowledge() return 5 items max | int | Limits search/list results to N items |
| `--title` | Title for added/updated item | `` | No title set or updated | str | Sets title for new item or updates existing item's title |
| `--source` | Optional source for added item | `` | No source recorded for new item | str | Sets metadata source field for new item (ignored on update) |
| `--tags` | Comma-separated tags for added item | `` | No tags applied | str | Parses CSV into tag list for new item; added to metadata['tags'] |
| `--metadata` | Raw JSON metadata string (overrides source/tags on add, appends/replaces on update) | `` | No metadata override; uses source/tags as specified | str | Parses as JSON; on add: replaces all metadata; on update: appends/merges with existing |
| `--importance` | Importance score for update (0.0 to 1.0) | `-1.0` | No importance change on update; -1.0 signals no-op to update_knowledge() | float | Sets item importance to specified value (0.0–1.0 range) |
| `--reembed` | Force vector re-embedding during update | `False` | Update does not re-embed vectors | store_true | Forces vector re-embedding during update via update_knowledge() |
| `--hard` | Permanently delete from database (requires exact string 'WIPE') | None | Soft-delete only (tombstone); data recoverable | str | Hard-delete (permanent) only if value is exactly "WIPE"; any other value errors |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes this run against PATH for all DB reads/writes. |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg, resolve_venv_python)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `memory.knowledge_helpers (add_knowledge, delete_knowledge, get_all_types, list_knowledge, search_knowledge, update_knowledge)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
