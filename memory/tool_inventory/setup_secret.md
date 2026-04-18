---
tool: bin/setup_secret.py
sha1: 54a4dc30d9d9
mtime_utc: 2026-04-18T03:18:29.586755+00:00
generated_utc: 2026-04-18T05:16:53.221485+00:00
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
| `--list` | list stored services (no values) | — |  | store_true |  |
| `--delete` | remove a service from the vault | — |  |  |  |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `auth_utils (DB_PATH, get_api_key, get_master_key, set_api_key, _get_fernet)`

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 117)
- `sqlite3.connect()  → `DB_PATH`` (line 141)
- `sqlite3.connect()  → `DB_PATH`` (line 230)
- `sqlite3.connect()  → `DB_PATH`` (line 95)


## Notable external imports

- `getpass`

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
