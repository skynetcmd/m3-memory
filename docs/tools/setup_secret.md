---
tool: bin/setup_secret.py
sha1: e8a4f8c2dc53
mtime_utc: 2026-05-01T09:15:53.144020+00:00
generated_utc: 2026-05-01T13:05:27.061553+00:00
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

---

## Entry points

- `def main()` (line 286)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--list` | list stored services (no values) | `False` | Runs interactive service picker and secret entry flow (getpass-hidden input). | store_true | Displays table of vault entries (service name, version, origin device, updated_at); exits. |
| `--delete` | remove a service from the vault | — | Runs interactive service picker and secret entry flow (getpass-hidden input). | str | Deletes specified service after confirmation prompt; exits. |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes all DB reads/writes against PATH for this run. |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `auth_utils`
- `auth_utils (_get_fernet, _vault_db_path, get_api_key, get_master_key, set_api_key)`
- `m3_sdk (add_database_arg)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `_db_path()`` (line 106)
- `sqlite3.connect()  → `_db_path()`` (line 128)
- `sqlite3.connect()  → `_db_path()`` (line 152)
- `sqlite3.connect()  → `_db_path()`` (line 241)


---

## Notable external imports

- `getpass`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
