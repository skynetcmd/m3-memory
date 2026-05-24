---
tool: install_os.py
sha1: a6a84b7bb34a
mtime_utc: 2026-05-18T11:45:52.203952+00:00
generated_utc: 2026-05-24T12:09:08.811471+00:00
private: false
---

# install_os.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `def main()` (line 155)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `AGENT_OS_MASTER_KEY`
- `M3_MEMORY_ROOT`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['fnm', '--version']`` (line 115)
- `subprocess.run()  → `['node', '--version']`` (line 90)
- `subprocess.run()  → `['nvm', 'version']`` (line 81)
- `subprocess.run()  → `['winget', '--version']`` (line 100)
- `subprocess.run()  → `[python_exe, pg_sync_script]`` (line 217)
- `subprocess.run()  → `cmd`` (line 26)
- `subprocess.run()` (line 101)


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
