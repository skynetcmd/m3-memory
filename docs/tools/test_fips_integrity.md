---
tool: bin/test_fips_integrity.py
sha1: 132888b1948b
mtime_utc: 2026-05-07T00:55:26.901096+00:00
generated_utc: 2026-05-07T01:00:25.810831+00:00
private: false
---

# bin/test_fips_integrity.py

## Purpose

test_fips_integrity.py — Validation suite for FIPS-ready crypto abstraction.

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `auth_utils`
- `crypto_provider (provider, get_sha256)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `base64`
- `cryptography.fernet (Fernet)`
- `cryptography.hazmat.primitives (hashes)`
- `cryptography.hazmat.primitives.kdf.pbkdf2 (PBKDF2HMAC)`
- `ssl`
- `unittest`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
