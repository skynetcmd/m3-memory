---
tool: install_os.py
sha1: 403e5345d062
mtime_utc: 2026-09-13T16:58:32.230781+00:00
generated_utc: 2026-09-13T17:24:00.261524+00:00
private: false
---

# install_os.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `def main()` (line 330)
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

- `subprocess.run()  → `['fnm', '--version']`` (line 222)
- `subprocess.run()  → `['node', '--version']`` (line 197)
- `subprocess.run()  → `['nvm', 'version']`` (line 188)
- `subprocess.run()  → `['winget', '--version']`` (line 207)
- `subprocess.run()  → `[python_exe, pg_sync_script]`` (line 418)
- `subprocess.run()  → `cmd`` (line 100)
- `subprocess.run()` (line 208)


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
