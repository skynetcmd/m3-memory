---
tool: bin/setup_embedder.py
sha1: 84ea7b6dcc94
mtime_utc: 2026-05-07T03:32:14.563827+00:00
generated_utc: 2026-05-09T13:54:34.855221+00:00
private: false
---

# bin/setup_embedder.py

## Purpose

setup_embedder.py — Sovereign, air-gapped installation of local embedder (LM Studio + BGE-M3).
Usage: python bin/setup_embedder.py

---

## Entry points

- `def main()` (line 179)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `APPDATA`
- `LLM_ENDPOINTS_CSV`
- `M3_CRYPTO_BACKEND`

---

## Calls INTO this repo (intra-repo imports)

- `crypto_provider (get_sha256)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.Popen()  → `[str(bin_path), 'server', 'start', '--port', '8081']`` (line 157)
- `subprocess.run()  → `['launchctl', 'load', str(plist_path)]`` (line 127)
- `subprocess.run()  → `['launchctl', 'unload', str(plist_path)]`` (line 126)
- `subprocess.run()  → `['systemctl', '--user', 'daemon-reload']`` (line 148)
- `subprocess.run()  → `['systemctl', '--user', 'enable', 'm3-embedder']`` (line 149)
- `subprocess.run()  → `['systemctl', '--user', 'start', 'm3-embedder']`` (line 150)


---

## Notable external imports

- `http.client`
- `platform`

---

## File dependencies (repo paths referenced)

- `./fips-hash.sh`
- `manifest.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
