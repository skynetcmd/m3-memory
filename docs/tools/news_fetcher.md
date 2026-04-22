---
tool: bin/news_fetcher.py
sha1: 5e16daa75135
mtime_utc: 2026-04-22T01:03:02.048233+00:00
generated_utc: 2026-04-22T01:32:11.655978+00:00
private: false
---

# bin/news_fetcher.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

- `NEWS_API_KEY`

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

**http**

- `requests.get()  → `NEWS_API_URL`` (line 55)


## Notable external imports

- `mcp (FastMCP)`
- `requests`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
