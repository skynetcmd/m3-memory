---
tool: bin/test_fips_integrity.py
sha1: 3654cf2b80b2
mtime_utc: 2026-05-31T16:08:17.252572+00:00
generated_utc: 2026-05-31T18:42:53.011509+00:00
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

- `M3_CRYPTO_BACKEND`
- `M3_FIPS_MODE`

---

## Calls INTO this repo (intra-repo imports)

- `auth_utils`
- `crypto_provider (get_sha256, provider)`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `base64`
- `cryptography.fernet (Fernet)`
- `cryptography.hazmat.primitives (hashes)`
- `cryptography.hazmat.primitives.ciphers.aead (AESGCM)`
- `cryptography.hazmat.primitives.kdf.pbkdf2 (PBKDF2HMAC)`
- `ctypes`
- `ssl`
- `unittest`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
