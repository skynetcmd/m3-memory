---
tool: bin/m3_upgrade.py
sha1: f0edb5a6e670
mtime_utc: 2026-10-04T18:21:20.109200+00:00
generated_utc: 2026-10-04T18:21:26.606610+00:00
private: false
---

# bin/m3_upgrade.py

## Purpose

Upgrade m3-memory end to end, using the right command for how it was installed.

Run this INSTEAD of remembering a sequence:

    python bin/m3_upgrade.py            # detect, upgrade, finalize, verify
    python bin/m3_upgrade.py --dry-run  # print the plan, change nothing

Why a standalone script rather than an ``m3 upgrade`` subcommand: the upgrade
replaces the very package a subcommand would be running from. On Windows that is
a file-locking failure, not a theoretical one. This file deliberately does NOT
import ``m3_memory``, so the package can be swapped underneath it safely. It
shells out to the ``m3`` executable for the steps that need m3 itself, each in a
fresh process that loads whichever payload is current at that moment.

The steps mirror what the CLI's own help already tells you to do:
  1. ``m3 stop``   -- release DB-writer file locks. ``m3 stop --help`` says to do
                      this before upgrading on Windows.
  2. upgrade       -- pipx / pip / pip --user, chosen by DETECTION, never assumed.
  3. ``m3 stop``   -- AGAIN, now that the package is replaced. Anything still up
                      is running OLD code, and step 4 only restarts what it finds
                      STOPPED, so a survivor would be reported "running" and keep
                      serving stale code. The cognitive loop is the one that
                      matters: stale code there writes stale derived knowledge.
  4. ``m3 setup``  -- rewire agent configs, migrate schemas, restart services --
                      which brings the daemons back on the NEW version.
  5. ``m3 doctor --fix --fix-hooks``
                   -- verify AND repair, exiting nonzero if still unhappy. An
                      upgrade is the one moment repair is unambiguously wanted:
                      the user asked for a new version and is waiting. A bare
                      verify left self-repairable state broken behind a warning
                      (embed-server exec bit, dead agent MCP configs, a wedged
                      dashboard), and --fix-hooks matters because hook entries
                      point at the payload step 2 just REPLACED and nothing else
                      in the upgrade rewires them.

There is no ``m3 upgrade`` subcommand. Guessing one (or guessing ``pipx`` for a
pip install) is the failure this script exists to prevent: ``pipx upgrade``
against a pip install exits 0 having upgraded NOTHING, which reads as success.

---

## Entry points

- `def run()` (line 257)
- `def main()` (line 273)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--dry-run` | Show the plan; change nothing. | `False` |  | store_true |  |
| `--skip-stop` | Do not stop DB writers first. | `False` |  | store_true |  |
| `--yes` | Do not prompt (for scripted use). | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `[exe, '--version']`` (line 77)
- `subprocess.run()  → `[sys.executable, '-c', code]`` (line 156)
- `subprocess.run()  → `cmd`` (line 264)
- `subprocess.run()` (line 102)


---

## Notable external imports

- `site`

---

## File dependencies (repo paths referenced)

- `pipx_metadata.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
