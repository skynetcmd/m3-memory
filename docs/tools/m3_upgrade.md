---
tool: bin/m3_upgrade.py
sha1: 4869b8d6e5b4
mtime_utc: 2026-10-07T02:56:11.486071+00:00
generated_utc: 2026-10-07T02:56:21.041791+00:00
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

``m3 upgrade`` launches this script. Guessing ``pipx`` for a pip install is the
failure it exists to prevent: ``pipx upgrade`` against a pip install exits 0
having upgraded NOTHING, which reads as success.

On Windows, any process running from one of the venv's launcher .exe files
(``m3 upgrade``'s own launcher when ``~/.local/bin/m3.exe`` is a symlink, an
agent's m3 MCP server, a hook) blocks pip from replacing it, and pip then leaves
the package uninstalled. ``locked_launchers`` checks for that before anything
is stopped or replaced.

---

## Entry points

- `def run()` (line 531)
- `def main()` (line 638)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--dry-run` | Show the plan; change nothing. | `False` |  | store_true |  |
| `--skip-stop` | Do not stop DB writers first. | `False` |  | store_true |  |
| `--yes` | Do not prompt (for scripted use). | `False` |  | store_true |  |
| `--from-pypi` | pipx installs: reinstall from PyPI, moving the recorded source off a local wheel or path. | `False` |  | store_true |  |
| `--wait-for-pid` | argparse.SUPPRESS | `` |  | str |  |
| `--pause-at-end` | argparse.SUPPRESS | `False` |  | store_true |  |
| `--log` | argparse.SUPPRESS | `` |  | str |  |
| `--stop-agents` | Windows: stop m3 in agent sessions that hold its launcher (they reconnect afterwards). Asked interactively otherwise. | `False` |  | store_true |  |
| `--stop-pids` | argparse.SUPPRESS | `` |  | str |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.Popen()  → `cmd`` (line 483)
- `subprocess.Popen()  → `cmd`` (line 541)
- `subprocess.run()  → `[exe, '--version']`` (line 104)
- `subprocess.run()  → `[sys.executable, '-c', code]`` (line 185)
- `subprocess.run()  → `cmd`` (line 551)
- `subprocess.run()  → `cmd`` (line 572)
- `subprocess.run()` (line 129)
- `subprocess.run()` (line 847)


---

## Notable external imports

- `ctypes`
- `ctypes (wintypes)`
- `psutil`
- `site`

---

## File dependencies (repo paths referenced)

- `m3_memory-*.dist-info/entry_points.txt`
- `pipx_metadata.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
