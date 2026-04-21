---
tool: bin/chatlog_core.py
sha1: a28167caa15b
mtime_utc: 2026-04-21T21:11:14.772645+00:00
generated_utc: 2026-04-21T21:26:01.772227+00:00
private: false
---

# bin/chatlog_core.py

## Purpose

chatlog_core.py — the load-bearing module for the chat log subsystem.

Provides:
- Async write queue (asyncio.Queue) with flush-on-size/interval
- Spill-to-disk backpressure at memory/chatlog_spill/YYYYMMDD.jsonl
- chatlog_write_impl / chatlog_write_bulk_impl — enqueue + flush
- chatlog_search_impl — delegates to memory_core.memory_search_scored_impl
- chatlog_promote_impl — ATTACH DATABASE cross-DB copy (separate/hybrid) or UPDATE (integrated)
- chatlog_list_conversations_impl
- chatlog_cost_report_impl — aggregates tokens/cost from metadata_json
- chatlog_set_redaction_impl / chatlog_rescrub_impl
- PRICE_TABLE for client-side cost computation
- atexit + SIGTERM drain on shutdown

All paths route through M3Context.get_chatlog_conn() so integrated/separate/hybrid
modes are handled transparently.

## Entry points

- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `chatlog_config`
- `chatlog_redaction`
- `m3_sdk (M3Context)`
- `m3_sdk (M3Context, resolve_db_path)`
- `m3_sdk (active_database, M3Context)`
- `m3_sdk (resolve_db_path)`
- `memory_core`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

- `atexit`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
