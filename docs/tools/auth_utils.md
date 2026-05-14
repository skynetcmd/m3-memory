---
tool: bin/auth_utils.py
sha1: 95168c771ffa
mtime_utc: 2026-05-14T06:03:06.905715+00:00
generated_utc: 2026-05-14T14:05:30.589295+00:00
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

- `_task_runtime (no_window_kwargs)`
- `crypto_provider (provider)`
- `m3_sdk (resolve_db_path)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['cmdkey', f'/list:{service}']`` (line 232)
- `subprocess.run()  → `['security', 'find-generic-password', '-s', 'AGENT_OS_MASTER_KEY', '-w']`` (line 61)
- `subprocess.run()  → `['security', 'find-generic-password', '-s', service, '-w']`` (line 218)
- `subprocess.run()` (line 247)

**sqlite**

- `sqlite3.connect()  → `_vault_db_path()`` (line 325)
- `sqlite3.connect()  → `vault_path`` (line 264)


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
