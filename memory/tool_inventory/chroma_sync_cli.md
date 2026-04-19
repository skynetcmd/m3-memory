---
tool: bin/chroma_sync_cli.py
sha1: 11dafadd01b5
mtime_utc: 2026-04-18T22:28:14.293115+00:00
generated_utc: 2026-04-19T00:39:15.968903+00:00
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
