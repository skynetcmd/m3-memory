#!/usr/bin/env python3
"""Interactive CLI for adding API keys to the m3-memory encrypted vault.

Keys are stored in the synchronized_secrets table via auth_utils.set_api_key,
which Fernet-encrypts the value against AGENT_OS_MASTER_KEY from the OS keyring.

Usage:
    python bin/setup_secret.py              # interactive add
    python bin/setup_secret.py --list       # show stored services (no values)
    python bin/setup_secret.py --delete KEY # remove one entry
"""
from __future__ import annotations

import argparse
import getpass
import os
import sqlite3
import sys

BIN_DIR = os.path.dirname(os.path.abspath(__file__))
if BIN_DIR not in sys.path:
    sys.path.insert(0, BIN_DIR)

from auth_utils import _get_fernet, _vault_db_path, get_api_key, get_master_key, set_api_key


def _db_path() -> str:
    """Resolve the vault DB path at call time so --database / M3_DATABASE win."""
    return _vault_db_path()


# Expose DB_PATH for backward compat with any helper scripts that imported it
# from here; it reflects the *default* location, not the active resolved path.
import auth_utils as _auth_utils

DB_PATH = _auth_utils.DB_PATH

# Known external services. Format validators return (ok, message).
KNOWN_SERVICES: list[dict] = [
    {
        "name": "OPENAI_API_KEY",
        "description": "OpenAI API key — used by eval judge and OpenAI LLM calls",
        "url": "https://platform.openai.com/api-keys",
        "validator": lambda v: (
            (True, "project-scoped key") if v.startswith("sk-proj-")
            else (True, "legacy key") if v.startswith("sk-")
            else (False, "expected prefix 'sk-' or 'sk-proj-'")
        ),
    },
    {
        "name": "ANTHROPIC_API_KEY",
        "description": "Anthropic API key — for Claude API calls",
        "url": "https://console.anthropic.com/settings/keys",
        "validator": lambda v: (
            (True, "looks like an Anthropic key") if v.startswith("sk-ant-")
            else (False, "expected prefix 'sk-ant-'")
        ),
    },
    {
        "name": "GEMINI_API_KEY",
        "description": "Google Gemini API key — for Gemini API calls",
        "url": "https://aistudio.google.com/apikey",
        "validator": lambda v: (
            (True, "looks like a Google API key") if v.startswith("AIza") and len(v) >= 35
            else (False, "expected prefix 'AIza' and length >= 35")
        ),
    },
    {
        "name": "XAI_API_KEY",
        "description": "xAI / Grok API key",
        "url": "https://console.x.ai",
        "validator": lambda v: (
            (True, "looks like an xAI key") if v.startswith("xai-")
            else (False, "expected prefix 'xai-'")
        ),
    },
]


def _fail(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def _check_master_key() -> None:
    if get_master_key():
        print("  AGENT_OS_MASTER_KEY: found")
        return
    print("  AGENT_OS_MASTER_KEY: MISSING")
    print()
    print("  The master key encrypts everything in the vault. Without it, set_api_key")
    print("  will fail. Generate one and store it in the OS keyring before continuing:")
    print()
    print("    python -c \"import secrets, keyring; keyring.set_password("
          "'system', 'AGENT_OS_MASTER_KEY', secrets.token_urlsafe(32))\"")
    print()
    print("  Back up the generated value to a password manager immediately — losing it")
    print("  makes every secret in the vault unrecoverable.")
    _fail("master key not configured")


def _list_vault() -> None:
    if not os.path.exists(_db_path()):
        print(f"vault empty (no database at {_db_path()})")
        return
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.execute(
            "SELECT service_name, version, origin_device, updated_at "
            "FROM synchronized_secrets ORDER BY service_name"
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    if not rows:
        print("vault empty")
        return
    name_w = max(len(r[0]) for r in rows)
    print(f"{'service'.ljust(name_w)}  version  origin_device              updated_at")
    print("-" * (name_w + 60))
    for name, version, device, updated in rows:
        print(f"{name.ljust(name_w)}  {str(version).rjust(7)}  {(device or '').ljust(24)}  {updated}")


def _delete_service(service: str) -> None:
    if not os.path.exists(_db_path()):
        _fail(f"vault database not found at {_db_path()}")
    conn = sqlite3.connect(_db_path())
    try:
        cur = conn.execute(
            "SELECT version, updated_at FROM synchronized_secrets WHERE service_name = ?",
            (service,),
        )
        row = cur.fetchone()
        if not row:
            _fail(f"no entry for service '{service}'")
        print(f"found {service}: version {row[0]}, last updated {row[1]}")
        confirm = input(f"delete {service}? [y/N]: ").strip().lower()
        if confirm != "y":
            print("aborted")
            return
        conn.execute("DELETE FROM synchronized_secrets WHERE service_name = ?", (service,))
        conn.commit()
        print(f"deleted {service}")
    finally:
        conn.close()


def _existing_info(service: str) -> tuple[int, str] | None:
    if not os.path.exists(_db_path()):
        return None
    conn = sqlite3.connect(_db_path())
    try:
        row = conn.execute(
            "SELECT version, updated_at FROM synchronized_secrets WHERE service_name = ?",
            (service,),
        ).fetchone()
        return (row[0], row[1]) if row else None
    finally:
        conn.close()


def _pick_service() -> tuple[str, callable | None, str | None]:
    print()
    print("Available services:")
    for i, svc in enumerate(KNOWN_SERVICES, start=1):
        print(f"  {i}. {svc['name'].ljust(20)} {svc['description']}")
    print(f"  {len(KNOWN_SERVICES) + 1}. (custom)          Enter a custom service name")
    print()
    choice = input(f"Choose [1-{len(KNOWN_SERVICES) + 1}]: ").strip()
    try:
        idx = int(choice)
    except ValueError:
        _fail("invalid selection")
    if idx == len(KNOWN_SERVICES) + 1:
        name = input("Service name (UPPER_SNAKE_CASE): ").strip().upper()
        if not name or not all(c.isalnum() or c == "_" for c in name):
            _fail("service name must be alphanumeric + underscores")
        return name, None, None
    if 1 <= idx <= len(KNOWN_SERVICES):
        svc = KNOWN_SERVICES[idx - 1]
        return svc["name"], svc["validator"], svc["url"]
    _fail("invalid selection")
    return "", None, None  # unreachable, keeps type checker happy


def _interactive_add() -> None:
    print("m3-memory secret setup")
    print()
    print("[1/4] Checking master key...")
    _check_master_key()

    name, validator, url = _pick_service()

    print()
    print(f"[2/4] Checking for existing {name}...")
    existing = _existing_info(name)
    if existing:
        version, updated = existing
        print(f"  existing entry found: version {version}, last updated {updated}")
        confirm = input("  replace existing key? [y/N]: ").strip().lower()
        if confirm != "y":
            print("  aborted — existing key unchanged")
            return
    else:
        print("  no existing entry")

    if url:
        print()
        print(f"  Get your key from: {url}")

    print()
    print("[3/4] Paste your API key (input is hidden and will NOT echo):")
    value = getpass.getpass("  > ").strip()
    if not value:
        _fail("empty key — aborted")
    if len(value) < 16:
        _fail(f"key looks too short ({len(value)} chars) — aborted without storing")

    if validator:
        ok, msg = validator(value)
        if ok:
            print(f"  format: {msg}")
        else:
            print(f"  warning: {msg}")
            confirm = input("  store anyway? [y/N]: ").strip().lower()
            if confirm != "y":
                print("  aborted")
                return

    print()
    print("[4/4] Encrypting and storing...")
    try:
        set_api_key(name, value)
    except Exception as exc:
        _fail(f"set_api_key failed: {type(exc).__name__}: {exc}")

    # Verify the vault round-trip directly — bypass get_api_key's 3-tier lookup,
    # which would return an env var or OS keyring value from an earlier tier.
    master_key = get_master_key()
    conn = sqlite3.connect(_db_path())
    try:
        row = conn.execute(
            "SELECT encrypted_value FROM synchronized_secrets WHERE service_name = ?",
            (name,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        _fail("vault round-trip failed: row missing after set_api_key")
    try:
        decrypted = _get_fernet(master_key).decrypt(row[0].encode("utf-8")).decode("utf-8")
    except Exception as exc:
        _fail(f"vault round-trip failed to decrypt: {type(exc).__name__}: {exc}")
    if decrypted != value:
        _fail("vault round-trip failed: decrypted value does not match stored value")

    prefix = decrypted[: min(8, len(decrypted))]
    print("  stored ✓")
    print(f"  vault verification: {len(decrypted)}-char value starting with '{prefix}...'")

    # Warn if an earlier resolution tier would shadow the vault value
    resolved = get_api_key(name)
    if resolved and resolved != value:
        print()
        print(f"  ⚠  heads up: get_api_key('{name}') currently returns a DIFFERENT value")
        print("     than what you just stored. An earlier tier (env var, Windows")
        print("     Credential Manager, macOS Keychain, or Linux Secret Service) is")
        print("     shadowing the vault. Code calling get_api_key will see that value,")
        print("     not the one you just stored. Remove the shadowing entry if you want")
        print("     the vault to take effect:")
        print(f"       • env var:     unset {name}  (or remove from shell profile)")
        print(f"       • Windows:     cmdkey /delete:{name}")
        print(f"       • macOS:       security delete-generic-password -s {name}")
        print(f"       • Linux:       secret-tool clear service {name}")

    print()
    print("Next steps:")
    print(f"  • Code can now call: from auth_utils import get_api_key; get_api_key('{name}')")
    if name == "OPENAI_API_KEY":
        print("  • Set a usage limit: https://platform.openai.com/settings/organization/limits")
    print("  • To rotate this key, re-run: python bin/setup_secret.py")
    print(f"  • To delete it:             python bin/setup_secret.py --delete {name}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Interactive CLI for adding API keys to the m3-memory encrypted vault.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--list", action="store_true", help="list stored services (no values)")
    parser.add_argument("--delete", metavar="SERVICE", help="remove a service from the vault")
    from m3_sdk import add_database_arg
    add_database_arg(parser)
    args = parser.parse_args()

    if args.database:
        os.environ["M3_DATABASE"] = args.database

    if args.list:
        _list_vault()
        return
    if args.delete:
        _delete_service(args.delete)
        return
    _interactive_add()


if __name__ == "__main__":
    main()
