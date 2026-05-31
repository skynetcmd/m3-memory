---
tool: bin/setup_hooks.py
sha1: 8cadc2d78d66
mtime_utc: 2026-05-31T16:08:17.252572+00:00
generated_utc: 2026-05-31T18:42:52.976908+00:00
private: false
---

# bin/setup_hooks.py

## Purpose

Enable the repo's shared git hooks for this clone.

Points git at the tracked .githooks/ directory so the pre-push drift +
leakage gate runs for every agent and human, regardless of which AGENTS
instruction file they read. Run once per clone:

    python bin/setup_hooks.py

Idempotent. Cross-platform (the pre-push hook is bash; on Windows it runs
under Git-for-Windows' bundled bash, which `git push` invokes automatically).

---

## Entry points

- `def main()` (line 23)
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

- `subprocess.run()  → `['git', 'config', 'core.hooksPath', '.githooks']`` (line 28)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
