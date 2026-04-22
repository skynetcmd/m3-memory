---
tool: run_tests.py
sha1: b61a821d76be
mtime_utc: 2026-04-18T20:37:50.814027+00:00
generated_utc: 2026-04-22T02:11:24.931920+00:00
private: false
---

# run_tests.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

- `def main()` (line 8)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `[str(venv_python), str(f_path)]`` (line 57)


## Notable external imports

- `platform`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
