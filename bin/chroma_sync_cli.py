#!/usr/bin/env python3
"""CLI wrapper for ChromaDB bi-directional sync.

Usage:
    chroma-sync              # bi-directional sync (push + pull all collections)
    chroma-sync push         # outbound only
    chroma-sync pull         # inbound only
    chroma-sync status       # show sync status
    chroma-sync --quiet      # suppress output (for cron)

Safe to run from cron — logs to stderr, exits 0 on success or graceful offline.
"""

import asyncio
import sys
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))


async def main() -> int:
    from memory_bridge import chroma_sync, sync_status

    args = sys.argv[1:]
    quiet = "--quiet" in args or "-q" in args
    args = [a for a in args if a not in ("--quiet", "-q")]

    if args and args[0] == "status":
        print(sync_status())
        return 0

    direction = "both"
    if args and args[0] in ("push", "pull", "both"):
        direction = args[0]

    result = await chroma_sync(max_items=100, direction=direction, reset_stalled=True)

    if not quiet:
        print(result)
        print()
        print(sync_status())

    return 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:
        print(f"chroma-sync error: {type(exc).__name__}", file=sys.stderr)
        code = 1
    sys.exit(code)
