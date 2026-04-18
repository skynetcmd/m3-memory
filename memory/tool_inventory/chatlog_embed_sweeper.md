---
tool: bin/chatlog_embed_sweeper.py
sha1: b30ba03bbdac
mtime_utc: 2026-04-18T15:41:31.006444+00:00
generated_utc: 2026-04-18T16:33:21.595895+00:00
private: false
---

# bin/chatlog_embed_sweeper.py

## Purpose

chatlog_embed_sweeper.py — lazy embed chat log rows missing embeddings.

Runs on a schedule (default every 30 min via install_schedules.py). Picks up
rows written with embed=False, embeds in batches using memory_core._embed_many,
and drains any spill-to-disk files from the async write queue.

## Entry points

- `async def main()` (line 251)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--batch` | Batch size (default from config.embed_sweeper.batch_size) | None (uses cfg.embed_sweeper.batch_size) | Uses config batch size | int | Embeds N rows per batch iteration |
| `--max-per-run` | Max rows per run (default from CHATLOG_EMBED_MAX_PER_RUN env or 10000) | None (uses CHATLOG_EMBED_MAX_PER_RUN env or 10000) | Embeds up to 10000 rows per run | int | Processes up to N rows total per invocation |
| `--dry-run` | Query and log but don't embed | — | Queries unembed rows, embeds in batches, updates DB | store_true | Queries and logs row counts without embedding |
| `--drain-spill` | Process spill files before embedding | — | Skips spill drain (unless spill dir exists) | store_true | Always processes spill files before embedding |

## Environment variables read

- `CHATLOG_EMBED_MAX_PER_RUN`

## Calls INTO this repo (intra-repo imports)

- `chatlog_config`
- `embedding_utils (pack)`
- `memory_core (_embed_many)`

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 295)


## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
