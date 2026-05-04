---
tool: bin/m3_autoenrich.py
sha1: 67a2e0b74158
mtime_utc: 2026-05-04T22:05:11.189182+00:00
generated_utc: 2026-05-04T22:24:29.207100+00:00
private: false
---

# bin/m3_autoenrich.py

## Purpose

Toggle the M3_AUTO_ENRICH env var on/off, cross-platform.

On every invocation, flips the persistent value: ON -> OFF or OFF -> ON.
After flipping, prints the exact command to revert (so a script log captures
both states).

Persistence:
  - Windows: User scope via `setx` (HKCU\Environment). Persists across sessions.
              Note: existing processes do NOT pick up the new value; only new
              processes inherit it.
  - macOS/Linux: a single-line `export M3_AUTO_ENRICH=...` in
              ~/.config/m3-memory/env, sourced by adding one line to your
              shell rc the first time. Subsequent toggles only rewrite the env
              file; shell rc is left alone.

Detection of current state reads the persistent store, NOT os.environ — that
way the result is consistent regardless of which shell launched this script.

---

## Entry points

- `def main()` (line 145)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--status` | Print current state and exit (no flip). | `False` |  | store_true |  |
| `--on` | Force on regardless of current state. | `False` |  | store_true |  |
| `--off` | Force off regardless of current state. | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['reg', 'delete', 'HKCU\\Environment', '/F', '/V', VAR]`` (line 65)
- `subprocess.run()  → `['setx', VAR, value]`` (line 71)


---

## Notable external imports

- `platform`
- `winreg`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
