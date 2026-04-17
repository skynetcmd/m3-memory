---
tool: bin/secret_rotator.py
sha1: bf6e966bbec0
mtime_utc: 2026-04-06T00:25:00.987104+00:00
generated_utc: 2026-04-17T04:17:01.761969+00:00
private: false
---

# bin/secret_rotator.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--dry-run` |  | — |  | store_true |  |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `auth_utils (set_api_key)`
- `m3_sdk (M3Context)`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

- `secrets`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
