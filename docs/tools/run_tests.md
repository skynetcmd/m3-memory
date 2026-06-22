---
tool: run_tests.py
sha1: 1d13b4233717
mtime_utc: 2026-06-09T04:46:44.852093+00:00
generated_utc: 2026-06-12T20:00:05.774967+00:00
private: false
---

# run_tests.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `def main()` (line 8)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `[str(venv_python), str(f_path)]`` (line 57)


---

## Notable external imports

- `platform`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
