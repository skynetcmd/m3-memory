---
tool: bin/deep_sync.py
sha1: a4db0b124814
mtime_utc: 2026-04-22T01:03:02.028648+00:00
generated_utc: 2026-05-01T13:05:26.784275+00:00
private: false
---

# bin/deep_sync.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

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

- `subprocess.check_call()  → `['git', '-C', WORKSPACE] + args`` (line 17)
- `subprocess.run()  → `[cleanup_script]`` (line 29)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `cleanup_logs.sh`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
