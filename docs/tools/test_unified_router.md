---
tool: bin/test_unified_router.py
sha1: e977c088014d
mtime_utc: 2026-04-22T01:03:02.056394+00:00
generated_utc: 2026-04-22T01:32:11.722781+00:00
private: false
---

# bin/test_unified_router.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

- `ANTHROPIC_API_KEY`
- `GEMINI_API_KEY`
- `LM_API_TOKEN`

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

**http**

- `requests.post()  → `ROUTER_URL`` (line 27)


## Notable external imports

- `requests`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
