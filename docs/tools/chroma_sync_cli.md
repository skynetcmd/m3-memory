---
tool: bin/chroma_sync_cli.py
sha1: 1ae5f8f1bb43
mtime_utc: 2026-04-21T20:44:36.924503+00:00
generated_utc: 2026-04-21T21:22:27.047064+00:00
private: false
---

# bin/chroma_sync_cli.py

## Purpose

CLI wrapper for ChromaDB bi-directional sync.

Usage:
    chroma-sync              # bi-directional sync (push + pull all collections)
    chroma-sync push         # outbound only
    chroma-sync pull         # inbound only
    chroma-sync status       # show sync status
    chroma-sync --quiet      # suppress output (for cron)

Safe to run from cron — logs to stderr, exits 0 on success or graceful offline.

## Entry points

- `async def main()` (line 22)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `memory_bridge (chroma_sync, sync_status)`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
