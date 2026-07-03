---
tool: bin/install_schedules.py
sha1: 21bbf5924475
mtime_utc: 2026-07-02T01:21:24.693175+00:00
generated_utc: 2026-07-03T20:00:03.438071+00:00
private: false
---

# bin/install_schedules.py

## Purpose

M3 Memory: Cross-Platform Schedule Installer.
Automatically configures crontab (macOS/Linux) or schtasks (Windows).
Uses project virtual environment paths and ensures log directories exist.

---

## Entry points

- `def main()` (line 726)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--list` | List configured schedules and exit. | `False` | Prints "Nothing to do" message and exits. | store_true | Lists all 5 schedules (auditor, sync, maintenance, rotator, chatlog-embed-sweep). |
| `--add` | Install one schedule by name (e.g. chatlog-embed-sweep) or 'all'. | — | Prints "Nothing to do" message and exits. | str | Installs Windows Task(s) or crontab entries matching NAME; 'all' installs all 5. |
| `--remove` | Remove one schedule by name, or 'all'. | — | Prints "Nothing to do" message and exits. | str | Removes Windows Task(s) matching NAME; 'all' removes all; Unix users edit crontab. |
| `--repair` | Re-install every configured schedule in place (alias for --add all). | `False` |  | store_true |  |
| `--verify` | Verify the registered job(s) match the spec (Windows task / macOS launchd / Linux systemd). NAME or 'all' (default). Exit code is non-zero if verification fails. | — |  | str |  |

---

## Environment variables read

- `USERDOMAIN`
- `USERNAME`

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (ensure_governor_config)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['crontab', '-l']`` (line 59)
- `subprocess.run()  → `['crontab', tmp_path]`` (line 78)
- `subprocess.run()  → `['launchctl', 'list']`` (line 658)
- `subprocess.run()  → `['launchctl', 'load', dest]`` (line 120)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 119)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 156)
- `subprocess.run()  → `['schtasks', '/Create', '/TN', task['name'], '/XML', xml_path, '/F']`` (line 569)
- `subprocess.run()  → `['schtasks', '/Delete', '/TN', task['name'], '/F']`` (line 549)
- `subprocess.run()  → `['schtasks', '/Delete', '/TN', task['name'], '/F']`` (line 605)
- `subprocess.run()  → `['schtasks', '/Query', '/TN', name, '/XML', 'ONE']`` (line 620)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 136)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 170)
- `subprocess.run()  → `['systemctl', '--user', 'disable', '--now', 'm3-cognitive-loop.service']`` (line 164)
- `subprocess.run()  → `['systemctl', '--user', 'enable', '--now', 'm3-cognitive-loop.service']`` (line 137)
- `subprocess.run()  → `['systemctl', '--user', 'is-active', unit]`` (line 678)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `crontab.template`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
