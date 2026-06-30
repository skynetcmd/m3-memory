---
tool: bin/install_schedules.py
sha1: 0b7ca632b73e
mtime_utc: 2026-06-30T21:32:48.328242+00:00
generated_utc: 2026-06-30T22:19:18.271724+00:00
private: false
---

# bin/install_schedules.py

## Purpose

M3 Memory: Cross-Platform Schedule Installer.
Automatically configures crontab (macOS/Linux) or schtasks (Windows).
Uses project virtual environment paths and ensures log directories exist.

---

## Entry points

- `def main()` (line 418)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--list` | List configured schedules and exit. | `False` | Prints "Nothing to do" message and exits. | store_true | Lists all 5 schedules (auditor, sync, maintenance, rotator, chatlog-embed-sweep). |
| `--add` | Install one schedule by name (e.g. chatlog-embed-sweep) or 'all'. | — | Prints "Nothing to do" message and exits. | str | Installs Windows Task(s) or crontab entries matching NAME; 'all' installs all 5. |
| `--remove` | Remove one schedule by name, or 'all'. | — | Prints "Nothing to do" message and exits. | str | Removes Windows Task(s) matching NAME; 'all' removes all; Unix users edit crontab. |
| `--repair` | Re-install every configured schedule in place (alias for --add all). | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (ensure_governor_config)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['crontab', '-l']`` (line 59)
- `subprocess.run()  → `['crontab', tmp_path]`` (line 78)
- `subprocess.run()  → `['launchctl', 'load', dest]`` (line 120)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 119)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 156)
- `subprocess.run()  → `['powershell', '-NoProfile', '-NonInteractive', '-Command', ps]`` (line 377)
- `subprocess.run()  → `['schtasks', '/Delete', '/TN', task['name'], '/F']`` (line 334)
- `subprocess.run()  → `['schtasks', '/Delete', '/TN', task['name'], '/F']`` (line 398)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 136)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 170)
- `subprocess.run()  → `['systemctl', '--user', 'disable', '--now', 'm3-cognitive-loop.service']`` (line 164)
- `subprocess.run()  → `['systemctl', '--user', 'enable', '--now', 'm3-cognitive-loop.service']`` (line 137)
- `subprocess.run()  → `schtasks_cmd`` (line 360)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `crontab.template`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
