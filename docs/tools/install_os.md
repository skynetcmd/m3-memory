---
tool: install_os.py
sha1: c033deaa7abf
mtime_utc: 2026-04-11T18:30:40.239795+00:00
generated_utc: 2026-05-01T13:05:27.197022+00:00
private: false
---

# install_os.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `def main()` (line 112)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `AGENT_OS_MASTER_KEY`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['fnm', '--version']`` (line 106)
- `subprocess.run()  → `['node', '--version']`` (line 81)
- `subprocess.run()  → `['nvm', 'version']`` (line 72)
- `subprocess.run()  → `['winget', '--version']`` (line 91)
- `subprocess.run()  → `[python_exe, pg_sync_script]`` (line 171)
- `subprocess.run()  → `cmd`` (line 17)
- `subprocess.run()` (line 92)


---

## Notable external imports

- `getpass`
- `platform`
- `venv`

---

## File dependencies (repo paths referenced)

- `requirements.txt`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
