from __future__ import annotations

import base64
import logging
import os
import platform
import re
import sqlite3
import subprocess
import unicodedata
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Dynamically resolve DB path relative to project root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "memory", "agent_memory.db")


def get_master_key() -> str | None:
    """Retrieves the AGENT_OS_MASTER_KEY from the native OS keyring or environment."""
    val = os.getenv("AGENT_OS_MASTER_KEY", "").strip()
    if val:
        return val

    try:
        import keyring
        val = keyring.get_password("system", "AGENT_OS_MASTER_KEY")
        if val:
            return val
    except Exception:
        pass

    if platform.system() == "Darwin":
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", "AGENT_OS_MASTER_KEY", "-w"],
                capture_output=True, text=True, check=True, timeout=5
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
    salt_path = os.path.join(os.path.expanduser("~"), ".agent_os_salt")
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
    try:
        with open(salt_path, "wb") as f:
            f.write(salt)
        # Set restrictive permissions (user read/write only)
        if platform.system() != "Windows":
            os.chmod(salt_path, 0o600)
    except Exception as e:
        logger.warning(f"Could not save device salt to {salt_path}: {e}")
    return salt


_PBKDF2_ITERATIONS = 600_000
_PBKDF2_LEGACY_ITERATIONS = 100_000

def _get_fernet(master_key: str, iterations: int = _PBKDF2_ITERATIONS):
    """
    Derives a Fernet encryption object from the master key using PBKDF2HMAC.
    Uses a per-device salt for protection against rainbow tables.
    """
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    salt = _get_device_salt()
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    key = base64.urlsafe_b64encode(kdf.derive(master_key.encode("utf-8")))
    return Fernet(key)


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

    # Try python keyring if available
    system = platform.system()
    try:
        import keyring
        val = keyring.get_password("system", service)
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
                capture_output=True, text=True, check=True, timeout=5
            )
            return result.stdout.strip()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass

    elif system == "Windows":
        # Windows Credential Manager via cmdkey (fallback when keyring module unavailable)
        try:
            result = subprocess.run(
                ["cmdkey", f"/list:{service}"],
                capture_output=True, text=True, timeout=5
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
                capture_output=True, text=True, timeout=3
            )
            if "org.freedesktop.secrets" not in result.stdout:
                logger.debug("Linux Secret Service (D-Bus) not running — skipping keyring")
        except (FileNotFoundError, subprocess.TimeoutExpired):
            logger.debug("D-Bus not available on this Linux system")

    # Fallback to the synchronized encrypted vault
    if os.path.exists(DB_PATH):
        conn = None
        try:
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT encrypted_value FROM synchronized_secrets WHERE service_name = ?", (service,))
            row = cur.fetchone()

            if row and row[0]:
                master_key = get_master_key()
                if not master_key:
                    logger.warning(f"Found encrypted secret for {service}, but AGENT_OS_MASTER_KEY is missing from keyring.")
                    return None

                # Try current iteration count first, fall back to legacy
                try:
                    fernet = _get_fernet(master_key)
                    decrypted = fernet.decrypt(row[0].encode("utf-8")).decode("utf-8")
                except Exception:
                    try:
                        fernet_legacy = _get_fernet(master_key, iterations=_PBKDF2_LEGACY_ITERATIONS)
                        decrypted = fernet_legacy.decrypt(row[0].encode("utf-8")).decode("utf-8")
                        logger.warning(f"Secret '{service}' decrypted with legacy PBKDF2 iterations. Auto-migrating to {_PBKDF2_ITERATIONS} iterations.")
                        # Auto-migrate: re-encrypt with current iterations
                        try:
                            fernet_new = _get_fernet(master_key)
                            new_encrypted = fernet_new.encrypt(decrypted.encode("utf-8")).decode("utf-8")
                            cur.execute("UPDATE synchronized_secrets SET encrypted_value = ? WHERE service_name = ?", (new_encrypted, service))
                            conn.commit()
                        except Exception as mig_err:
                            logger.debug(f"Auto-migration of '{service}' failed: {mig_err}")
                    except Exception:
                        logger.debug(f"Failed to decrypt vault secret for {service}")
                        return None
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

    fernet = _get_fernet(master_key)
    encrypted_value = fernet.encrypt(value.encode("utf-8")).decode("utf-8")
    origin_device = os.environ.get("ORIGIN_DEVICE", platform.node())
    # ISO-8601 UTC timestamp
    now = datetime.now(timezone.utc).isoformat()

    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
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
