---
tool: bin/auth_utils.py
sha1: f9462952c90e
mtime_utc: 2026-04-07T04:04:38.923774+00:00
generated_utc: 2026-04-18T05:16:53.065128+00:00
private: false
---

# bin/auth_utils.py

## Purpose

Cross-platform credential resolution and management with AES-256 Fernet encryption via PBKDF2 (600K iterations). Resolves secrets from environment → OS keyring → encrypted SQLite vault with auto-migration support.

## Entry points / Public API

- `get_master_key()` (line 20) — retrieves AGENT_OS_MASTER_KEY from env, OS keyring, or macOS Keychain
- `get_api_key(service)` (line 107) — priority chain: env → keyring → native platform store → encrypted vault
- `set_api_key(service, value)` (line 221) — encrypts and stores API key to synchronized_secrets table with versioning
- `_get_fernet(master_key, iterations)` (line 79) — derives Fernet cipher from master key using PBKDF2HMAC
- `_get_device_salt()` (line 47) — persistent 16-byte salt from `~/.agent_os_salt` (generated if missing, mode 0o600)
- `_sanitize_service(service)` (line 99) — removes injection-unsafe chars via NFKC normalization + whitelist

## CLI flags / arguments

_(no CLI surface — invoked as a library/module)_

## Environment variables read

- `AGENT_OS_MASTER_KEY` — primary master key (checked first before keyring)
- `LM_STUDIO_API_KEY` — fallback for `LM_API_TOKEN` service lookup
- `ORIGIN_DEVICE` — device identifier for versioned vault upserts (defaults to `platform.node()`)
- M3_VAULT_* keys — none; vault is SQLite-based, not env-keyed

## Calls INTO this repo (intra-repo imports)

_(none detected)_

## Calls OUT (external side-channels)

**subprocess** (platform-specific credential stores)
- `security find-generic-password` → macOS Keychain (lines 36, 141)
- `cmdkey /list:` → Windows Credential Manager check (line 152)
- `dbus-send` → Linux Secret Service availability probe (line 166)

**sqlite3**
- `sqlite3.connect(DB_PATH)` → `memory/agent_memory.db` reads/writes to `synchronized_secrets` table (lines 181, 238)

**keyring** (optional, cross-platform)
- `keyring.get_password("system", service)` (line 129)

**cryptography**
- `Fernet`, `PBKDF2HMAC(SHA256, length=32, iterations=600000)`, device salt-based KDF

**platform, unicodedata, base64, logging**

## File dependencies (repo paths referenced)

- `memory/agent_memory.db` — synchronized_secrets table (service_name, encrypted_value, version, origin_device, updated_at, PRIMARY KEY(service_name))
- `~/.agent_os_salt` — 16-byte persistent device salt (created on first use)

## Re-validation

If `sha1` differs from current file's hash, the inventory is stale. Confirm flags, env vars, entry-points, calls, and regenerate via `python bin/gen_tool_inventory.py`.
