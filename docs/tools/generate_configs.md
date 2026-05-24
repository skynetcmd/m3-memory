---
tool: bin/generate_configs.py
sha1: cbf2299d1cc4
mtime_utc: 2026-05-21T15:00:20.157251+00:00
generated_utc: 2026-05-24T12:09:07.884297+00:00
private: false
---

# bin/generate_configs.py

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

- `m3_sdk (get_m3_root)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `.aider.conf.yml`
- `.mcp.json`
- `claude-settings.json`
- `gemini-settings.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
