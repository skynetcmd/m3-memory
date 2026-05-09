---
tool: bin/fetch_sovereign_assets.py
sha1: 019e20067347
mtime_utc: 2026-05-07T03:32:14.556216+00:00
generated_utc: 2026-05-09T13:54:34.165911+00:00
private: false
---

# bin/fetch_sovereign_assets.py

## Purpose

fetch_sovereign_assets.py — Hydrate the _assets/embedder directory for sovereign setup.
Usage: python bin/fetch_sovereign_assets.py

---

## Entry points

- `def main()` (line 55)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `crypto_provider (get_sha256)`

---

## Calls OUT (external side-channels)

**http**

- `requests.get()  → `url`` (line 41)


---

## Notable external imports

- `requests`
- `tqdm (tqdm)`

---

## File dependencies (repo paths referenced)

- `manifest.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
