---
tool: bin/thermal_utils.py
sha1: a5394ce425a9
mtime_utc: 2026-07-02T01:21:24.724041+00:00
generated_utc: 2026-07-03T20:00:04.079760+00:00
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

- `subprocess.run()  → `['sysctl', '-n', 'kern.thermal_pressure']`` (line 36)
- `subprocess.run()` (line 51)
- `subprocess.run()` (line 68)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
