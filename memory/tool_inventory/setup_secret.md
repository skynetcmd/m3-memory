---
tool: bin/setup_secret.py
sha1: 67aff86c730a
mtime_utc: 2026-04-17T04:16:28.315598+00:00
generated_utc: 2026-04-17T04:17:01.765886+00:00
private: false
---

# bin/setup_secret.py

## Purpose

Interactive CLI for adding API keys to the m3-memory encrypted vault.

Keys are stored in the synchronized_secrets table via auth_utils.set_api_key,
which Fernet-encrypts the value against AGENT_OS_MASTER_KEY from the OS keyring.

Usage:
    python bin/setup_secret.py              # interactive add
    python bin/setup_secret.py --list       # show stored services (no values)
    python bin/setup_secret.py --delete KEY # remove one entry

## Entry points

- `def main()` (line 275)
- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--list` | list stored services (no values) | — | lists all stored API keys (no values shown) | store_true | runs `_list_vault()` |
| `--delete` | remove a service from the vault | — | enter interactive add mode | metavar=SERVICE | prompts for confirmation, deletes via `_delete_service()` |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `auth_utils (DB_PATH, get_api_key, get_master_key, set_api_key, _get_fernet)`

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()` → `synchronized_secrets` table in `DB_PATH` (line 95, line 117, line 141, line 230)
  - Reads: `service_name, version, origin_device, updated_at` for listing
  - Writes: INSERT/UPDATE via `auth_utils.set_api_key()`, DELETE for removal
  - Verifies round-trip encryption of stored secrets

**OS keyring** (via `auth_utils`)

- `get_master_key()` reads `AGENT_OS_MASTER_KEY` from system keyring for Fernet decryption


## Notable external imports

- `getpass`

## File dependencies (repo paths referenced)

- `bin/auth_utils.py` (imports: DB_PATH, get_api_key, get_master_key, set_api_key, _get_fernet)

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
