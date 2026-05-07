#!/usr/bin/env python3
"""
test_fips_integrity.py — Validation suite for FIPS-ready crypto abstraction.
"""

import base64
import hashlib
import os
import sys
import unittest
from pathlib import Path

# Add bin to path
sys.path.insert(0, str(Path(__file__).parent))

import auth_utils
from crypto_provider import get_sha256, provider


class TestFipsIntegrity(unittest.TestCase):
    def test_sha256_abstraction(self):
        """Verify that get_sha256 matches standard hashlib."""
        data = b"M3 Sovereign FIPS Test"
        expected = hashlib.sha256(data).hexdigest()
        actual = get_sha256(data)
        self.assertEqual(actual, expected, "SHA-256 abstraction mismatch")

    def test_aes_gcm_functionality(self):
        """Verify AES-256-GCM encryption and decryption."""
        key = os.urandom(32)
        data = b"Secret knowledge for sovereign memory"

        # Test Provider Directly
        encrypted = provider.encrypt(data, key)
        self.assertNotEqual(encrypted, data)

        decrypted = provider.decrypt(encrypted, key)
        self.assertEqual(decrypted, data, "AES-GCM decryption failed")

    def test_key_access_wrappers(self):
        """Verify that lock/unlock wrappers don't crash and maintain state."""
        # This tests that the methods exist and execute without error
        try:
            provider.unlock_key()
            # Do nothing
            provider.lock_key()
            success = True
        except Exception:
            success = False
        self.assertTrue(success, "Key access wrappers raised an exception")

    def test_secret_migration_logic(self):
        """Verify that auth_utils can decrypt legacy Fernet and modern GCM."""
        master_key = "test-master-key"
        secret_value = "fips-ready-token-123"  # nosec B105 - test fixture, not a real credential

        # 1. Create a legacy Fernet token manually
        # auth_utils uses _PBKDF2_ITERATIONS (600k)
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        salt = auth_utils._get_device_salt()
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=600_000,
        )
        raw_key = kdf.derive(master_key.encode("utf-8"))
        f_key = base64.urlsafe_b64encode(raw_key)
        legacy_token = Fernet(f_key).encrypt(secret_value.encode("utf-8"))
        legacy_token_str = base64.b64encode(legacy_token).decode("utf-8")

        # 2. Test decryption of legacy token
        decrypted_legacy = auth_utils._decrypt_token(legacy_token_str, master_key)
        self.assertEqual(decrypted_legacy, secret_value, "Failed to decrypt legacy Fernet token")

        # 3. Create a modern GCM token
        modern_token_str = auth_utils._encrypt_value(secret_value, master_key)
        self.assertFalse(modern_token_str.startswith("gAAAA"), "Modern token should not have Fernet signature")

        # 4. Test decryption of modern token
        decrypted_modern = auth_utils._decrypt_token(modern_token_str, master_key)
        self.assertEqual(decrypted_modern, secret_value, "Failed to decrypt modern GCM token")

    def test_ssl_context_hardening(self):
        """Verify that the generated SSL context is hardened."""
        ctx = provider.get_ssl_context()
        import ssl

        if hasattr(ssl, "TLSVersion"):
            # Should be at least TLS 1.2, preferred 1.3
            self.assertGreaterEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_2)

        # Should not allow old versions
        if hasattr(ssl, "OP_NO_TLSv1"):
            self.assertTrue(ctx.options & ssl.OP_NO_TLSv1)

if __name__ == "__main__":
    print(f"--- FIPS Readiness Validation (Backend: {provider.backend}) ---")
    unittest.main()
