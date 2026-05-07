---
tool: bin/setup_embedder.py
sha1: 6bb944c91d29
mtime_utc: 2026-05-06T23:50:45.218960+00:00
generated_utc: 2026-05-06T23:57:12.635587+00:00
private: false
---

# bin/setup_embedder.py

## Purpose

setup_embedder.py — Sovereign, air-gapped installation of local embedder (LM Studio + BGE-M3).
Usage: python bin/setup_embedder.py

---

## Entry points

- `def main()` (line 169)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `APPDATA`
- `LLM_ENDPOINTS_CSV`

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.Popen()  → `[str(bin_path), 'server', 'start', '--port', '8081']`` (line 147)
- `subprocess.run()  → `['launchctl', 'load', str(plist_path)]`` (line 117)
- `subprocess.run()  → `['launchctl', 'unload', str(plist_path)]`` (line 116)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 138)
- `subprocess.run()  → `['systemctl', '--user', 'enable', 'm3-embedder']`` (line 139)
- `subprocess.run()  → `['systemctl', '--user', 'start', 'm3-embedder']`` (line 140)


---

## Notable external imports

- `http.client`
- `platform`

---

## File dependencies (repo paths referenced)

- `manifest.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
