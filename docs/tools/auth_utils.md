---
tool: bin/auth_utils.py
sha1: abb1762f6b62
mtime_utc: 2026-09-08T04:58:30.665100+00:00
generated_utc: 2026-09-08T05:08:53.548117+00:00
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
- `COMPUTERNAME`
- `HOSTNAME`
- `LM_STUDIO_API_KEY`
- `M3_AGENT_OS_SALT_HEX`

---

## Calls INTO this repo (intra-repo imports)

- `_task_runtime (no_window_kwargs)`
- `crypto_provider (provider)`
- `m3_sdk (get_m3_config_root, get_m3_root)`
- `m3_sdk (getenv_compat)`
- `m3_sdk (resolve_db_path)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.run()  → `['cmdkey', f'/list:{service}']`` (line 404)
- `subprocess.run()  → `['security', 'find-generic-password', '-s', 'AGENT_OS_MASTER_KEY', '-w']`` (line 151)
- `subprocess.run()  → `['security', 'find-generic-password', '-s', service, '-w']`` (line 390)
- `subprocess.run()` (line 419)

**sqlite**

- `sqlite3.connect()  → `_vault_db_path()`` (line 503)
- `sqlite3.connect()  → `vault_path`` (line 436)


---

## Notable external imports

- `base64`
- `cryptography.fernet (Fernet)`
- `keyring`
- `memory.backends (active_backend)`
- `platform`
- `unicodedata`

---

## File dependencies (repo paths referenced)

- `agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
