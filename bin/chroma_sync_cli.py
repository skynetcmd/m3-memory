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
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))


async def main() -> int:
    args = sys.argv[1:]
    quiet = "--quiet" in args or "-q" in args
    args = [a for a in args if a not in ("--quiet", "-q")]

    # --database overrides M3_DATABASE for this run only. Parse before importing
    # memory_bridge so memory_core's default M3Context picks up the new path.
    for i, a in enumerate(list(args)):
        if a == "--database" and i + 1 < len(args):
            os.environ["M3_DATABASE"] = args[i + 1]
            del args[i : i + 2]
            break
        if a.startswith("--database="):
            os.environ["M3_DATABASE"] = a.split("=", 1)[1]
            args.remove(a)
            break

    from memory_bridge import chroma_sync, sync_status

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
