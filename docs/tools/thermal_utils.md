---
tool: bin/thermal_utils.py
sha1: 494e0223501a
mtime_utc: 2026-08-31T02:53:49.345403+00:00
generated_utc: 2026-08-31T02:55:06.157165+00:00
private: false
---

# bin/thermal_utils.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

_(no conventional entry point detected)_

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `_task_runtime (no_window_kwargs)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['sysctl', '-n', 'kern.thermal_pressure']`` (line 91)
- `subprocess.run()` (line 106)
- `subprocess.run()` (line 124)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
