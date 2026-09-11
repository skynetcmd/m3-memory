---
tool: install_os.py
sha1: 053db4a0a641
mtime_utc: 2026-09-11T11:57:11.489302+00:00
generated_utc: 2026-09-11T13:11:51.333275+00:00
private: false
---

# install_os.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `def main()` (line 299)
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

- `subprocess.run()  → `['fnm', '--version']`` (line 191)
- `subprocess.run()  → `['node', '--version']`` (line 166)
- `subprocess.run()  → `['nvm', 'version']`` (line 157)
- `subprocess.run()  → `['winget', '--version']`` (line 176)
- `subprocess.run()  → `[python_exe, pg_sync_script]`` (line 373)
- `subprocess.run()  → `cmd`` (line 70)
- `subprocess.run()` (line 177)


---

## Notable external imports

- `getpass`
- `ntpath`
- `posixpath`
- `venv`

---

## File dependencies (repo paths referenced)

- `requirements.txt`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
