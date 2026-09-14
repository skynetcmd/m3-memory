---
tool: bin/install_schedules.py
sha1: 35d35488cd64
mtime_utc: 2026-09-14T03:47:49.138379+00:00
generated_utc: 2026-09-14T03:48:49.642230+00:00
private: false
---

# bin/install_schedules.py

## Purpose

M3 Memory: Cross-Platform Schedule Installer.
Automatically configures crontab (macOS/Linux) or schtasks (Windows).
Uses project virtual environment paths and ensures log directories exist.

---

## Entry points

- `def main()` (line 1979)
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
- `subprocess.run()  → `['launchctl', 'list']`` (line 1894)
- `subprocess.run()  → `['launchctl', 'list']`` (line 311)
- `subprocess.run()  → `['launchctl', 'load', dest]`` (line 163)
- `subprocess.run()  → `['launchctl', 'load', dest]`` (line 215)
- `subprocess.run()  → `['launchctl', 'load', dest]`` (line 455)
- `subprocess.run()  → `['launchctl', 'load', dest]`` (line 561)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 162)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 214)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 454)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 560)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 578)
- `subprocess.run()  → `['launchctl', 'unload', dest]`` (line 592)
- `subprocess.run()  → `['plutil', '-extract', 'KeepAlive', 'raw', '-o', '-', dest]`` (line 1907)
- `subprocess.run()  → `['sc.exe', 'query', _WINDOWS_RUST_EMBED_SERVICE]`` (line 353)
- `subprocess.run()  → `['schtasks', '/Create', '/TN', task['name'], '/XML', xml_path, '/F']`` (line 1694)
- `subprocess.run()  → `['schtasks', '/Delete', '/TN', task['name'], '/F']`` (line 1674)
- `subprocess.run()  → `['schtasks', '/Delete', '/TN', task['name'], '/F']`` (line 1813)
- `subprocess.run()  → `['schtasks', '/Query', '/TN', name, '/XML', 'ONE']`` (line 1828)
- `subprocess.run()  → `['schtasks', '/Query', '/TN', name, '/XML']`` (line 1412)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 179)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 234)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 472)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 504)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 534)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 608)
- `subprocess.run()  → `['systemctl', '--user', 'disable', '--now', 'm3-cognitive-loop.service']`` (line 602)
- `subprocess.run()  → `['systemctl', '--user', 'disable', '--now', 'm3-loop-watchdog.timer']`` (line 525)
- `subprocess.run()  → `['systemctl', '--user', 'enable', '--now', 'm3-cognitive-loop.service']`` (line 235)
- `subprocess.run()  → `['systemctl', '--user', 'enable', '--now', 'm3-dashboard.service']`` (line 180)
- `subprocess.run()  → `['systemctl', '--user', 'enable', '--now', 'm3-embed-server.service']`` (line 473)
- `subprocess.run()  → `['systemctl', '--user', 'enable', '--now', 'm3-loop-watchdog.timer']`` (line 508)
- `subprocess.run()  → `['systemctl', '--user', 'is-active', unit]`` (line 1931)
- `subprocess.run()  → `['systemctl', '--user', 'show', '-p', 'LoadState', '--value', name]`` (line 1297)
- `subprocess.run()  → `[probe, '-c', 'import m3_memory']`` (line 646)
- `subprocess.run()  → `cmd`` (line 1356)
- `subprocess.run()  → `cmd`` (line 1497)


---

## Notable external imports

- `m3_core.autonomy (ensure_autonomy_config)`
- `memory.backends (dialect)`
- `memory.orchestration (_db)`
- `memory.orchestration (agent_list_impl)`

---

## File dependencies (repo paths referenced)

- `crontab.template`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
