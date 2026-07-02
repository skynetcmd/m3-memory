from __future__ import annotations

import base64
import logging
import os
import threading

from crypto_provider import provider as crypto
from m3_sdk import getenv_compat

# --- Global Sync ---
_crypto_lock = threading.Lock()
import platform
import queue
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone

from _task_runtime import no_window_kwargs

logger = logging.getLogger(__name__)

# Keyring Circuit Breaker & Concurrency state
_KEYRING_CB_OPEN_UNTIL = 0.0
_KEYRING_LOCK = threading.Lock()

def safe_keyring_get_password(service_name: str, username: str) -> str | None:
    """Wraps keyring.get_password with a single-concurrency lock and 2s timeout.

    If it times out or fails, opens Keyring Circuit Breaker for 300s and returns None.
    """
    global _KEYRING_CB_OPEN_UNTIL

    # 1. Check Circuit Breaker
    if time.time() < _KEYRING_CB_OPEN_UNTIL:
        logger.debug("Keyring circuit breaker is OPEN. Falling back to local vault.")
        return None

    # 2. Acquire lock to serialize keyring queries
    acquired = _KEYRING_LOCK.acquire(timeout=2.0)
    if not acquired:
        logger.warning("Keyring lock contention. Keyring circuit breaker opened.")
        _KEYRING_CB_OPEN_UNTIL = time.time() + 300.0
        return None

    try:
        import keyring
        res_queue: "queue.Queue[tuple]" = queue.Queue()

        def _target():
            try:
                val = keyring.get_password(service_name, username)
                res_queue.put((True, val))
            except Exception as e:
                res_queue.put((False, e))

        t = threading.Thread(target=_target, daemon=True)
        t.start()

        try:
            success, result = res_queue.get(timeout=2.0)
            if success:
                return result
            else:
                logger.debug(f"Keyring lookup failed: {result}")
                return None
        except queue.Empty:
            logger.warning("Keyring lookup timed out after 2s. Opening circuit breaker for 300s.")
            _KEYRING_CB_OPEN_UNTIL = time.time() + 300.0
            return None
    except Exception as e:
        logger.debug(f"Keyring import or setup failed: {e}")
        return None
    finally:
        _KEYRING_LOCK.release()

# Dynamically resolve DB path relative to project root.
# DB_PATH is the *default* location kept for legacy callers; vault reads and
# writes below go through _vault_db_path() so that M3_DATABASE / --database
# overrides on the surrounding CLI or MCP tool flow through here too.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "memory", "agent_memory.db")


def _vault_db_path() -> str:
    """Resolve the active DB path for vault reads/writes.

    Lazy import to avoid the auth_utils ↔ m3_sdk circular dependency at
    module-load time. If m3_sdk isn't on sys.path yet (rare — mostly during
    installer bootstraps), fall back to DB_PATH.
    """
    try:
        from m3_sdk import resolve_db_path
        return resolve_db_path(None)
    except ImportError:
        return DB_PATH


def get_master_key() -> str | None:
    """Retrieves the AGENT_OS_MASTER_KEY from the native OS keyring or environment."""
    val = os.getenv("AGENT_OS_MASTER_KEY", "").strip()
    if val:
        return val

    try:
        val = safe_keyring_get_password("system", "AGENT_OS_MASTER_KEY")
        if val:
            return val
    except Exception:
        pass

    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "AGENT_OS_MASTER_KEY", "-w"],
                capture_output=True, text=True, check=True, timeout=5,
                **no_window_kwargs(),
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass

    return None


def _get_device_salt() -> bytes:
    """
    Retrieves a persistent 16-byte salt for PBKDF2 from a local file.
    Generates one if it doesn't exist. This ensures consistent key derivation
    on the same device without storing the salt in the DB.
    """
    from m3_sdk import get_m3_config_root, get_m3_root
    # Check config root first, fallback to legacy root
    config_root = get_m3_config_root()
    salt_path = os.path.join(config_root, ".agent_os_salt")
    if not os.path.exists(salt_path):
        legacy_path = os.path.join(get_m3_root(), ".agent_os_salt")
        if os.path.exists(legacy_path):
            salt_path = legacy_path

    if os.path.exists(salt_path):
        try:
            with open(salt_path, "rb") as f:
                salt = f.read(16)
                if len(salt) == 16:
                    return salt
        except Exception:
            pass

    # Generate new salt
    salt = os.urandom(16)
    # Ensure config directory exists
    os.makedirs(os.path.dirname(salt_path), exist_ok=True)
    try:
        with open(salt_path, "wb") as f:
            f.write(salt)
        # Set restrictive permissions (user read/write only).
        # Use os.name, not platform.system(): on Python 3.14 / Windows the
        # latter triggers a WMI query (platform._wmi_query) that can hang
        # indefinitely. os.name == "nt" is a constant — no WMI, no stall —
        # and chmod is a POSIX concern anyway.
        if os.name != "nt":
            os.chmod(salt_path, 0o600)
    except Exception as e:
        logger.warning(f"Could not save device salt to {salt_path}: {e}")
    return salt


_PBKDF2_ITERATIONS = 600_000
_PBKDF2_LEGACY_ITERATIONS = 100_000

def _derive_raw_key(master_key: str, iterations: int = _PBKDF2_ITERATIONS) -> bytes:
    """
    Derives a raw 32-byte key from the master key using PBKDF2-HMAC-SHA256.
    Uses a per-device salt for protection against rainbow tables.

    Routed through crypto_provider so key derivation honors the FIPS boundary
    (M3_FIPS_MODE -> wolfCrypt; fatal on any miss). Output is byte-identical to
    the prior direct PBKDF2HMAC path on the DEFAULT backend, so existing
    encrypted secrets decrypt unchanged.
    """
    from crypto_provider import provider

    salt = _get_device_salt()
    return provider.pbkdf2_sha256(master_key.encode("utf-8"), salt, iterations, 32)


def _get_fernet(master_key: str, iterations: int = _PBKDF2_ITERATIONS):
    """
    Derives a Fernet encryption object from the master key (legacy decrypt path).
    Uses a per-device salt for protection against rainbow tables.

    The KDF is routed through crypto_provider (FIPS boundary); Fernet itself is
    the non-FIPS legacy cipher and is reached only for decrypting pre-migration
    secrets — never for new writes (those use AES-256-GCM). Under M3_FIPS_STRICT
    this path is refused entirely (see FIPS_MODULE_BOUNDARY.md).
    """
    from crypto_provider import provider
    from cryptography.fernet import Fernet

    salt = _get_device_salt()
    raw = provider.pbkdf2_sha256(master_key.encode("utf-8"), salt, iterations, 32)
    key = base64.urlsafe_b64encode(raw)
    return Fernet(key)


def _decrypt_token(token_str: str, master_key: str, iterations: int = _PBKDF2_ITERATIONS) -> str | None:
    """Decrypts a token string using AES-GCM or Fernet (legacy). Protected by global sync."""
    try:
        raw_token = base64.b64decode(token_str)
        key = _derive_raw_key(master_key, iterations)

        with _crypto_lock:
            # Try modern AES-GCM first
            try:
                crypto.unlock_key()
                return crypto.decrypt(raw_token, key).decode("utf-8")
            except Exception:
                # Try legacy Fernet
                from cryptography.fernet import Fernet
                f_key = base64.urlsafe_b64encode(key)
                return Fernet(f_key).decrypt(raw_token).decode("utf-8")
            finally:
                crypto.lock_key()
    except Exception:
        return None

def _encrypt_value(value: str, master_key: str) -> str:
    """Encrypts a value using the modern AES-GCM provider. Protected by global sync."""
    key = _derive_raw_key(master_key)
    with _crypto_lock:
        try:
            crypto.unlock_key()
            encrypted = crypto.encrypt(value.encode("utf-8"), key)
            return base64.b64encode(encrypted).decode("utf-8")
        finally:
            crypto.lock_key()


def _sanitize_service(service: str) -> str:
    """
    Removes any characters that could be used for shell or PowerShell injection.
    Uses NFKC normalization to prevent bypass via similar-looking Unicode chars (H10).
    """
    normalized = unicodedata.normalize('NFKC', service)
    return re.sub(r'[^a-zA-Z0-9_\-]', '', normalized)

def get_api_key(service: str) -> str | None:
    """
    Resolves an API key by service name across macOS, Windows, and Linux.
    1. os.environ
    2. Python keyring (Windows Credential Manager, Linux Secret Service, macOS Keychain)
    3. Direct macOS Keychain fallback
    4. Encrypted SQLite Vault (synchronized_secrets)
    """
    service = _sanitize_service(service)
    val = os.getenv(service, "").strip()
    if val:
        return val

    if service == "LM_API_TOKEN":
        alt = os.getenv("LM_STUDIO_API_KEY", "").strip()
        if alt:
            return alt

    # Try python keyring if available.
    # Derive the OS name from sys.platform, not platform.system(): the latter
    # can hang on a WMI query on Python 3.14 / Windows. This runs on every
    # secret read, so it must not stall.
    system = {"darwin": "Darwin", "win32": "Windows"}.get(sys.platform, "Linux")
    try:
        val = safe_keyring_get_password("system", service)
        if val:
            return val
    except ImportError:
        logger.debug("keyring module not available, falling back to native tools.")
    except Exception as e:
        logger.debug(f"keyring failed: {e}")

    # Platform-specific native credential store fallbacks
    if system == "Darwin":
        # macOS Keychain via security CLI
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True, text=True, check=True, timeout=5,
                **no_window_kwargs(),
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass

    elif system == "Windows":
        # Windows Credential Manager via cmdkey (fallback when keyring module
        # unavailable). cmdkey.exe is a console binary — no_window_kwargs()
        # keeps it from flashing a window on scheduled-task runs.
        try:
            result = subprocess.run(
                ["cmdkey", f"/list:{service}"],
                capture_output=True, text=True, timeout=5,
                **no_window_kwargs(),
            )
            if service.lower() in result.stdout.lower() and result.returncode == 0:
                # Note: Get-StoredCredential is not a standard PS cmdlet.
                # Fall through to encrypted SQLite vault (cross-platform).
                logger.debug(f"Credential '{service}' exists in Windows Credential Manager but retrieval requires vault fallback")
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            logger.debug(f"Windows Credential Manager fallback failed for {service}")

    elif system == "Linux":
        # Check if D-Bus Secret Service is available before silent failures
        try:
            result = subprocess.run(
                ["dbus-send", "--session", "--dest=org.freedesktop.DBus",
                 "--type=method_call", "--print-reply",
                 "/org/freedesktop/DBus", "org.freedesktop.DBus.ListNames"],
                capture_output=True, text=True, timeout=3,
                **no_window_kwargs(),
            )
            if "org.freedesktop.secrets" not in result.stdout:
                logger.debug("Linux Secret Service (D-Bus) not running — skipping keyring")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.debug("D-Bus not available on this Linux system")

    # Fallback to the synchronized encrypted vault
    vault_path = _vault_db_path()
    if os.path.exists(vault_path):
        conn = None
        try:
            conn = sqlite3.connect(vault_path)
            cur = conn.cursor()
            cur.execute("SELECT encrypted_value FROM synchronized_secrets WHERE service_name = ?", (service,))
            row = cur.fetchone()

            if row and row[0]:
                master_key = get_master_key()
                if not master_key:
                    logger.warning(f"Found encrypted secret for {service}, but AGENT_OS_MASTER_KEY is missing from keyring.")
                    return None

                # Try current iteration count first, fall back to legacy
                decrypted = _decrypt_token(row[0], master_key)
                is_legacy = row[0].startswith("gAAAA")
                needs_migration = is_legacy

                if decrypted is None:
                    # Try legacy PBKDF2 iterations
                    decrypted = _decrypt_token(row[0], master_key, iterations=_PBKDF2_LEGACY_ITERATIONS)
                    if decrypted:
                        logger.warning(f"Secret '{service}' decrypted with legacy iterations. Auto-migrating.")
                        needs_migration = True
                    else:
                        logger.debug(f"Failed to decrypt vault secret for {service}")
                        return None

                # Auto-migrate if it was legacy Fernet OR legacy iterations
                if decrypted and needs_migration:
                    try:
                        new_encrypted = _encrypt_value(decrypted, master_key)
                        cur.execute("UPDATE synchronized_secrets SET encrypted_value = ? WHERE service_name = ?", (new_encrypted, service))
                        conn.commit()
                        logger.info(f"Auto-migrated '{service}' to modern AES-GCM encryption.")
                    except Exception as mig_err:
                        logger.debug(f"Auto-migration of '{service}' failed: {mig_err}")

                return decrypted
        except Exception as exc:
            logger.debug(f"Failed to read from encrypted vault: {type(exc).__name__}")
        finally:
            if conn:
                conn.close()

    return None

def set_api_key(service: str, value: str):
    """
    Encrypts and saves an API key to the synchronized_secrets vault in SQLite.
    Requires AGENT_OS_MASTER_KEY in the native keyring.
    """
    master_key = get_master_key()
    if not master_key:
        raise ValueError("AGENT_OS_MASTER_KEY not found in OS keyring. Cannot encrypt secret.")

    encrypted_value = _encrypt_value(value, master_key)
    origin_device = getenv_compat("M3_ORIGIN_DEVICE", "ORIGIN_DEVICE") or os.environ.get("COMPUTERNAME") or os.environ.get("HOSTNAME") or platform.node()
    # ISO-8601 UTC timestamp
    now = datetime.now(timezone.utc).isoformat()

    conn = None
    try:
        conn = sqlite3.connect(_vault_db_path())
        cur = conn.cursor()

        # Get current version, increment by 1
        cur.execute("SELECT version FROM synchronized_secrets WHERE service_name = ?", (service,))
        row = cur.fetchone()
        version = (row[0] + 1) if row else 1

        cur.execute("""
            INSERT INTO synchronized_secrets (service_name, encrypted_value, version, origin_device, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(service_name) DO UPDATE SET
                encrypted_value = excluded.encrypted_value,
                version = excluded.version,
                origin_device = excluded.origin_device,
                updated_at = excluded.updated_at
        """, (service, encrypted_value, version, origin_device, now))

        conn.commit()
        logger.info(f"Successfully saved encrypted {service} to the synchronized vault (version {version}).")
    finally:
        if conn:
            conn.close()
