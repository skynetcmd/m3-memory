---
tool: validate_env.py
sha1: 49544ecaa611
mtime_utc: 2026-06-28T12:28:55.249264+00:00
generated_utc: 2026-06-30T22:19:18.651959+00:00
private: false
---

# validate_env.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `def main()` (line 65)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `-l`, `--list` | List values of environment variables (e.g., -l secrets) | — | Validates the current env against the ENV_VARS manifest and reports errors. | str | With MODE=secrets, prints each tracked env var and its current value; exits 0. Unknown MODE exits 1 with a hint. |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
