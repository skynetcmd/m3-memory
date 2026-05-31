---
tool: bin/mission_control.py
sha1: b5e2f364909f
mtime_utc: 2026-05-30T20:48:00.265055+00:00
generated_utc: 2026-05-31T18:42:52.930423+00:00
private: true
---

# bin/mission_control.py

## Purpose

mission_control.py — Cross-platform pulse dashboard (macOS / Windows / Linux).
Run:  python bin/mission_control.py

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `LM_API_TOKEN`
- `LM_STUDIO_API_KEY`
- `PROCESSOR_ARCHITECTURE`
- `TERM_PROGRAM`

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (resolve_db_path)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.check_output()  → `['ioreg', '-r', '-d', '1', '-w', '0', '-c', 'IOAccelerator']`` (line 148)
- `subprocess.check_output()  → `['nvidia-smi', '--query-gpu=utilization.gpu', '--format=csv,noheader,nounits']`` (line 171)
- `subprocess.check_output()  → `['nvidia-smi', '--query-gpu=utilization.gpu', '--format=csv,noheader,nounits']`` (line 185)
- `subprocess.check_output()  → `['nvidia-smi']`` (line 233)
- `subprocess.check_output()  → `['security', 'find-generic-password', '-s', 'LM_STUDIO_API_KEY', '-w']`` (line 82)
- `subprocess.check_output()  → `['sysctl', '-n', 'machdep.cpu.brand_string']`` (line 297)
- `subprocess.check_output()  → `['system_profiler', 'SPDisplaysDataType']`` (line 349)
- `subprocess.check_output()  → `['system_profiler', 'SPHardwareDataType']`` (line 303)
- `subprocess.check_output()  → `cmd`` (line 257)
- `subprocess.check_output()` (line 213)
- `subprocess.check_output()` (line 288)
- `subprocess.check_output()` (line 338)

**http**

- `requests.get()  → `http://127.0.0.1:1234/api/v0/models`` (line 110)

**sqlite**

- `sqlite3.connect()  → `str(DB_PATH)`` (line 367)
- `sqlite3.connect()  → `str(DB_PATH)`` (line 392)


---

## Notable external imports

- `ctypes`
- `keyring`
- `platform`
- `psutil`
- `requests`
- `wmi`

---

## File dependencies (repo paths referenced)

- `agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
