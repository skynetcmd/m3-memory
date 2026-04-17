---
tool: bin/install_schedules.py
sha1: 72e5704fae15
mtime_utc: 2026-04-11T18:30:35.531344+00:00
generated_utc: 2026-04-17T04:17:01.701153+00:00
private: false
---

# bin/install_schedules.py

## Purpose

M3 Memory: Cross-Platform Schedule Installer.
Automatically configures crontab (macOS/Linux) or schtasks (Windows).
Uses project virtual environment paths and ensures log directories exist.

## Entry points

- `def main()` (line 132)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['crontab', '-l']`` (line 34)
- `subprocess.run()  → `['crontab', tmp_path]`` (line 53)
- `subprocess.run()  → `['schtasks', '/Delete', '/TN', task['name'], '/F']`` (line 107)
- `subprocess.run()  → `schtasks_cmd`` (line 121)


## Notable external imports

- `platform`

## File dependencies (repo paths referenced)

- `crontab.template`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
