---
tool: bin/macbook_status_server.py
sha1: 276ef0285aef
mtime_utc: 2026-05-23T12:31:13.384084+00:00
generated_utc: 2026-05-23T17:51:49.174883+00:00
private: true
---

# bin/macbook_status_server.py

## Purpose

MacBook network & LM Studio status server for Homepage dashboard.
Listens on port 9876. Returns JSON at /status with interface and LM Studio info.

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
- `MACBOOK_STATUS_HOST`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['ifconfig', iface]`` (line 28)
- `subprocess.run()  → `['security', 'find-generic-password', '-s', 'LM_STUDIO_API_KEY', '-w']`` (line 46)


---

## Notable external imports

- `http.server`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
