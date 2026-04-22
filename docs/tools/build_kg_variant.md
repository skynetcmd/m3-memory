---
tool: bin/build_kg_variant.py
sha1: 45cfd28fff87
mtime_utc: 2026-04-22T01:03:02.023346+00:00
generated_utc: 2026-04-22T01:22:54.461362+00:00
private: false
---

# bin/build_kg_variant.py

## Purpose

Build a KG-enriched variant from an existing source variant.

Duplicates memory_items + memory_embeddings under a new variant name (fresh IDs),
then populates memory_relationships with `related` edges computed from cosine
similarity on the duplicated embeddings. No LLM calls, no re-ingest.

Usage:
    python bin/build_kg_variant.py         --source-variant LME-ingestion         --target-variant LME-kg-sparse         --top-n 3 --sim-threshold 0.80

    python bin/build_kg_variant.py         --source-variant LME-ingestion         --target-variant LME-kg-dense         --top-n 8 --sim-threshold 0.70

## Entry points

- `def main()` (line 185)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--source-variant` |  | — |  | str |  |
| `--target-variant` |  | — |  | str |  |
| `--top-n` |  | — |  | int |  |
| `--sim-threshold` |  | — |  | float |  |
| `--wipe-target` | Delete any existing items/edges under target variant before building | `False` |  | store_true |  |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None |  | str |  |

## Environment variables read

- `AGENT_DB`

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg, resolve_db_path)`

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 197)


## Notable external imports

- `numpy`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
