---
tool: bin/generate_configs.py
sha1: 44eb90c6e663
mtime_utc: 2026-04-22T01:03:02.031002+00:00
generated_utc: 2026-04-22T01:32:11.532045+00:00
private: false
---

# bin/generate_configs.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

- `.aider.conf.yml`
- `.mcp.json`
- `claude-settings.json`
- `gemini-settings.json`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
