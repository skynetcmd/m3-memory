---
tool: install_os.py
sha1: d98f5dba3b3d
mtime_utc: 2026-06-23T02:21:18.043279+00:00
generated_utc: 2026-06-26T20:00:04.466464+00:00
private: false
---

# install_os.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `def main()` (line 235)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `AGENT_OS_MASTER_KEY`
- `M3_INSTALL_OXIDATION`
- `M3_MEMORY_ROOT`

---

## Calls INTO this repo (intra-repo imports)

- `m3_memory.rust_core_install (install_rust_core)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['fnm', '--version']`` (line 137)
- `subprocess.run()  → `['node', '--version']`` (line 112)
- `subprocess.run()  → `['nvm', 'version']`` (line 103)
- `subprocess.run()  → `['winget', '--version']`` (line 122)
- `subprocess.run()  → `[python_exe, pg_sync_script]`` (line 299)
- `subprocess.run()  → `cmd`` (line 48)
- `subprocess.run()` (line 123)


---

## Notable external imports

- `getpass`
- `venv`

---

## File dependencies (repo paths referenced)

- `requirements.txt`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
