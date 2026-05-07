---
tool: bin/fetch_sovereign_assets.py
sha1: 0c2e70a3c18f
mtime_utc: 2026-05-07T00:41:46.421083+00:00
generated_utc: 2026-05-07T00:43:52.339282+00:00
private: false
---

# bin/fetch_sovereign_assets.py

## Purpose

fetch_sovereign_assets.py — Hydrate the _assets/embedder directory for sovereign setup.
Usage: python bin/fetch_sovereign_assets.py

---

## Entry points

- `def main()` (line 43)
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

- `requests.get()  → `url`` (line 29)


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
