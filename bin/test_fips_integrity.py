#!/usr/bin/env python3
"""
test_fips_integrity.py — Validation suite for FIPS-ready crypto abstraction.
"""

import base64
import ctypes
import hashlib
import os
import sys
import unittest
from pathlib import Path


# Mock CDLL if real wolfSSL library is missing but requested
class MockCFunction:
    def __init__(self, func):
        self.func = func
        self.argtypes = []
        self.restype = None

    def __call__(self, *args, **kwargs):
        return self.func(*args, **kwargs)

class MockLibWolf:
    def __init__(self):
        self.active_keys = {}
        self.wolfCrypt_SetCb_fips = MockCFunction(self._wolfCrypt_SetCb_fips)
        self.wc_SetSeed_Cb = MockCFunction(self._wc_SetSeed_Cb)
        self.wc_GenerateSeed = MockCFunction(self._wc_GenerateSeed)
        self.wolfCrypt_GetStatus_fips = MockCFunction(self._wolfCrypt_GetStatus_fips)
        self.wc_AesGcmSetKey = MockCFunction(self._wc_AesGcmSetKey)
        self.wc_AesGcmEncrypt = MockCFunction(self._wc_AesGcmEncrypt)
        self.wc_AesGcmDecrypt = MockCFunction(self._wc_AesGcmDecrypt)
        self.wc_Sha256Hash = MockCFunction(self._wc_Sha256Hash)
        self.wc_PBKDF2 = MockCFunction(self._wc_PBKDF2)
        self.PRIVATE_KEY_LOCK = MockCFunction(self._PRIVATE_KEY_LOCK)
        self.PRIVATE_KEY_UNLOCK = MockCFunction(self._PRIVATE_KEY_UNLOCK)

    def _wolfCrypt_SetCb_fips(self, cb):
        return 0

    def _wc_SetSeed_Cb(self, cb):
        return 0

    def _wc_GenerateSeed(self, *args):
        return 0

    def _wolfCrypt_GetStatus_fips(self):
        return 0

    def _wc_AesGcmSetKey(self, aes, key, keySz):
        key_bytes = ctypes.string_at(key, keySz)
        aes_addr = ctypes.cast(aes, ctypes.c_void_p).value
        self.active_keys[aes_addr] = key_bytes
        return 0

    def _wc_AesGcmEncrypt(self, aes, out, in_buf, sz, iv, ivSz, tag, tagSz, authIn, authInSz):
        aes_addr = ctypes.cast(aes, ctypes.c_void_p).value
        key = self.active_keys.get(aes_addr)
        in_bytes = ctypes.string_at(in_buf, sz)
        iv_bytes = ctypes.string_at(iv, ivSz)

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        aesgcm = AESGCM(key)
        full_enc = aesgcm.encrypt(iv_bytes, in_bytes, None)

        ciphertext = full_enc[:-16]
        tag_bytes = full_enc[-16:]

        ctypes.memmove(out, ciphertext, len(ciphertext))
        ctypes.memmove(tag, tag_bytes, len(tag_bytes))
        return 0

    def _wc_AesGcmDecrypt(self, aes, out, in_buf, sz, iv, ivSz, tag, tagSz, authIn, authInSz):
        aes_addr = ctypes.cast(aes, ctypes.c_void_p).value
        key = self.active_keys.get(aes_addr)
        in_bytes = ctypes.string_at(in_buf, sz)
        iv_bytes = ctypes.string_at(iv, ivSz)
        tag_bytes = ctypes.string_at(tag, tagSz)

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        aesgcm = AESGCM(key)
        decrypted = aesgcm.decrypt(iv_bytes, in_bytes + tag_bytes, None)

        ctypes.memmove(out, decrypted, len(decrypted))
        return 0

    def _wc_Sha256Hash(self, in_buf, sz, out_buf):
        in_bytes = ctypes.string_at(in_buf, sz)
        h = hashlib.sha256(in_bytes).digest()
        ctypes.memmove(out_buf, h, 32)
        return 0

    def _wc_PBKDF2(self, out, passwd, pLen, salt, sLen, iters, kLen, typeH):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        pw = ctypes.string_at(passwd, pLen)
        slt = ctypes.string_at(salt, sLen)
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=kLen, salt=slt, iterations=iters)
        derived = kdf.derive(pw)
        ctypes.memmove(out, derived, kLen)
        return 0

    def _PRIVATE_KEY_LOCK(self):
        pass

    def _PRIVATE_KEY_UNLOCK(self):
        pass

if os.environ.get("M3_CRYPTO_BACKEND") == "WOLFSSL" or os.environ.get("M3_FIPS_MODE") == "1":
    try:
        lib_name = "libwolfssl.so" if os.name != "nt" else "wolfssl.dll"
        ctypes.CDLL(lib_name)
    except OSError:
        print("--- Real wolfSSL library not found. Applying MockLibWolf for unit test parity. ---")
        mock_lib = MockLibWolf()
        def mock_cdll(name, *args, **kwargs):
            if "wolfssl" in name.lower() or "libwolfssl" in name.lower():
                return mock_lib
            raise OSError(f"Mock CDLL: library {name} not found")
        ctypes.CDLL = mock_cdll  # type: ignore[assignment,misc]  # deliberate test monkeypatch of the CDLL loader

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
            # Should be TLS 1.3 exactly
            self.assertEqual(ctx.minimum_version, ssl.TLSVersion.TLSv1_3)
            self.assertEqual(ctx.maximum_version, ssl.TLSVersion.TLSv1_3)

        # Should not allow old versions
        if hasattr(ssl, "OP_NO_TLSv1"):
            self.assertTrue(ctx.options & ssl.OP_NO_TLSv1)

if __name__ == "__main__":
    print(f"--- FIPS Readiness Validation (Backend: {provider.backend}) ---")
    unittest.main()
