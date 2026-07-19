---
tool: bin/install_schedules.py
sha1: fd964e639585
mtime_utc: 2026-07-19T17:41:26.846998+00:00
generated_utc: 2026-07-19T19:29:22.312006+00:00
private: false
---

# bin/install_schedules.py

## Purpose

M3 Memory: Cross-Platform Schedule Installer.
Automatically configures crontab (macOS/Linux) or schtasks (Windows).
Uses project virtual environment paths and ensures log directories exist.

---

## Entry points

- `def main()` (line 883)
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
| `--port` | Port for the dashboard service (with --add dashboard). Default 8088. | `8088` |  | int |  |

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
- `subprocess.run()  → `['launchctl', 'list']`` (line 815)
- `subprocess.run()  → `['launchctl', 'load', dest]`` (line 127)
- `subprocess.run()  → `['launchctl', 'load', dest]`` (line 179)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 126)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 178)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 215)
- `subprocess.run()  → `['schtasks', '/Create', '/TN', task['name'], '/XML', xml_path, '/F']`` (line 692)
- `subprocess.run()  → `['schtasks', '/Delete', '/TN', task['name'], '/F']`` (line 672)
- `subprocess.run()  → `['schtasks', '/Delete', '/TN', task['name'], '/F']`` (line 762)
- `subprocess.run()  → `['schtasks', '/Query', '/TN', name, '/XML', 'ONE']`` (line 777)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 143)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 195)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 229)
- `subprocess.run()  → `['systemctl', '--user', 'disable', '--now', 'm3-cognitive-loop.service']`` (line 223)
- `subprocess.run()  → `['systemctl', '--user', 'enable', '--now', 'm3-cognitive-loop.service']`` (line 196)
- `subprocess.run()  → `['systemctl', '--user', 'enable', '--now', 'm3-dashboard.service']`` (line 144)
- `subprocess.run()  → `['systemctl', '--user', 'is-active', unit]`` (line 835)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `crontab.template`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
