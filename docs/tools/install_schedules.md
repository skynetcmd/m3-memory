---
tool: bin/install_schedules.py
sha1: 08489891b60e
mtime_utc: 2026-08-30T00:01:26.500435+00:00
generated_utc: 2026-08-30T01:24:29.151820+00:00
private: false
---

# bin/install_schedules.py

## Purpose

M3 Memory: Cross-Platform Schedule Installer.
Automatically configures crontab (macOS/Linux) or schtasks (Windows).
Uses project virtual environment paths and ensures log directories exist.

---

## Entry points

- `def main()` (line 1306)
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

- `M3_DASHBOARD_PORT`
- `USERDOMAIN`
- `USERNAME`

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (ensure_governor_config)`
- `m3_sdk (get_m3_engine_root)`
- `m3_sdk (kill_stale_daemons)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['crontab', '-l']`` (line 61)
- `subprocess.run()  → `['crontab', tmp_path]`` (line 114)
- `subprocess.run()  → `['launchctl', 'list']`` (line 1221)
- `subprocess.run()  → `['launchctl', 'load', dest]`` (line 163)
- `subprocess.run()  → `['launchctl', 'load', dest]`` (line 215)
- `subprocess.run()  → `['launchctl', 'load', dest]`` (line 324)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 162)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 214)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 323)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 341)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 355)
- `subprocess.run()  → `['plutil', '-extract', 'KeepAlive', 'raw', '-o', '-', dest]`` (line 1234)
- `subprocess.run()  → `['schtasks', '/Create', '/TN', task['name'], '/XML', xml_path, '/F']`` (line 1030)
- `subprocess.run()  → `['schtasks', '/Delete', '/TN', task['name'], '/F']`` (line 1010)
- `subprocess.run()  → `['schtasks', '/Delete', '/TN', task['name'], '/F']`` (line 1140)
- `subprocess.run()  → `['schtasks', '/Query', '/TN', name, '/XML', 'ONE']`` (line 1155)
- `subprocess.run()  → `['schtasks', '/Run', '/TN', name]`` (line 917)
- `subprocess.run()  → `['schtasks', '/Run', '/TN', name]`` (line 951)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 179)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 233)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 267)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 297)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 371)
- `subprocess.run()  → `['systemctl', '--user', 'disable', '--now', 'm3-cognitive-loop.service']`` (line 365)
- `subprocess.run()  → `['systemctl', '--user', 'disable', '--now', 'm3-loop-watchdog.timer']`` (line 288)
- `subprocess.run()  → `['systemctl', '--user', 'enable', '--now', 'm3-cognitive-loop.service']`` (line 234)
- `subprocess.run()  → `['systemctl', '--user', 'enable', '--now', 'm3-dashboard.service']`` (line 180)
- `subprocess.run()  → `['systemctl', '--user', 'enable', '--now', 'm3-loop-watchdog.timer']`` (line 271)
- `subprocess.run()  → `['systemctl', '--user', 'is-active', unit]`` (line 1258)
- `subprocess.run()  → `[probe, '-c', 'import httpx']`` (line 409)


---

## Notable external imports

- `m3_core.autonomy (ensure_autonomy_config)`

---

## File dependencies (repo paths referenced)

- `crontab.template`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
