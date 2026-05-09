---
tool: bin/auth_utils.py
sha1: adf379f8e153
mtime_utc: 2026-05-07T03:32:14.554106+00:00
generated_utc: 2026-05-09T13:54:33.765155+00:00
private: false
---

# bin/auth_utils.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

_(no conventional entry point detected)_

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `AGENT_OS_MASTER_KEY`
- `LM_STUDIO_API_KEY`
- `ORIGIN_DEVICE`

---

## Calls INTO this repo (intra-repo imports)

- `crypto_provider (provider)`
- `m3_sdk (resolve_db_path)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['cmdkey', f'/list:{service}']`` (line 226)
- `subprocess.run()  → `['security', 'find-generic-password', '-s', 'AGENT_OS_MASTER_KEY', '-w']`` (line 59)
- `subprocess.run()  → `['security', 'find-generic-password', '-s', service, '-w']`` (line 215)
- `subprocess.run()` (line 240)

**sqlite**

- `sqlite3.connect()  → `_vault_db_path()`` (line 317)
- `sqlite3.connect()  → `vault_path`` (line 256)


---

## Notable external imports

- `base64`
- `cryptography.fernet (Fernet)`
- `cryptography.hazmat.primitives (hashes)`
- `cryptography.hazmat.primitives.kdf.pbkdf2 (PBKDF2HMAC)`
- `keyring`
- `platform`
- `unicodedata`

---

## File dependencies (repo paths referenced)

- `agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
