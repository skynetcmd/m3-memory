---
tool: bin/auth_utils.py
sha1: f9462952c90e
mtime_utc: 2026-04-07T04:04:38.923774+00:00
generated_utc: 2026-04-18T16:33:21.556655+00:00
private: false
---

# bin/auth_utils.py

## Purpose

_(no module docstring — update the source file.)_

## Entry points

_(no conventional entry point detected)_

## CLI flags / arguments

_(no argparse arguments detected)_

## Environment variables read

- `AGENT_OS_MASTER_KEY`
- `LM_STUDIO_API_KEY`
- `ORIGIN_DEVICE`

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['cmdkey', f'/list:{service}']`` (line 152)
- `subprocess.run()  → `['security', 'find-generic-password', '-s', 'AGENT_OS_MASTER_KEY', '-w']`` (line 36)
- `subprocess.run()  → `['security', 'find-generic-password', '-s', service, '-w']`` (line 141)
- `subprocess.run()` (line 166)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 181)
- `sqlite3.connect()  → `DB_PATH`` (line 238)


## Notable external imports

- `base64`
- `cryptography.fernet (Fernet)`
- `cryptography.hazmat.primitives (hashes)`
- `cryptography.hazmat.primitives.kdf.pbkdf2 (PBKDF2HMAC)`
- `keyring`
- `platform`
- `unicodedata`

## File dependencies (repo paths referenced)

- `agent_memory.db`

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
