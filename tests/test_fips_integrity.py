"""
tests/test_fips_integrity.py — FIPS 140-3 boundary enforcement tests.

Exit criteria (Milestone 2):
  • All tests pass when M3_CRYPTO_BACKEND=DEFAULT (wolfSSL not required).
  • Under M3_FIPS_MODE=1, the provider either:
      a) Uses wolfSSL successfully (if the native library is present), OR
      b) Raises RuntimeError with a FATAL message (fail-closed enforcement).
  • SHA-256 and AES-256-GCM round-trips produce correct outputs on the
    DEFAULT backend (standard cryptography library).
  • No silent fallback from wolfSSL to stdlib under FIPS mode.
"""
import hashlib
import importlib
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _reload_provider(monkeypatch, backend=None, fips_mode=None):
    """Reload crypto_provider with specific env knobs."""
    if backend is not None:
        monkeypatch.setenv("M3_CRYPTO_BACKEND", backend)
    else:
        monkeypatch.delenv("M3_CRYPTO_BACKEND", raising=False)
    if fips_mode is not None:
        monkeypatch.setenv("M3_FIPS_MODE", fips_mode)
    else:
        monkeypatch.delenv("M3_FIPS_MODE", raising=False)

    # Reload so module-level BACKEND and provider re-evaluate env vars
    import crypto_provider
    importlib.reload(crypto_provider)
    return crypto_provider


# ── DEFAULT backend: correctness ───────────────────────────────────────────────

class TestDefaultBackend:
    """Standard library path — always available, no wolfSSL required."""

    def test_sha256_known_vector(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        data = b"abc"
        expected = hashlib.sha256(data).hexdigest()
        assert cp.provider.sha256(data) == expected

    def test_sha256_empty_input(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        expected = hashlib.sha256(b"").hexdigest()
        assert cp.provider.sha256(b"") == expected

    def test_encrypt_decrypt_roundtrip(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        key = os.urandom(32)  # AES-256
        plaintext = b"M3 FIPS test payload"
        token = cp.provider.encrypt(plaintext, key)
        recovered = cp.provider.decrypt(token, key)
        assert recovered == plaintext or recovered.rstrip(b"\x00") == plaintext

    def test_encrypt_produces_nonce_prefix(self, monkeypatch):
        """Ciphertext must be at least 12 (nonce) + 1 (data) + 16 (tag) bytes."""
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        key = os.urandom(32)
        token = cp.provider.encrypt(b"x", key)
        assert len(token) >= 29  # 12 nonce + 1 byte + 16 tag

    def test_wrong_key_raises(self, monkeypatch):
        """Decryption with a different key must fail (authentication tag mismatch)."""
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        key1 = os.urandom(32)
        key2 = os.urandom(32)
        token = cp.provider.encrypt(b"secret", key1)
        with pytest.raises(Exception):
            cp.provider.decrypt(token, key2)

    def test_backend_attribute(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        assert cp.provider.backend == "DEFAULT"

    def test_get_sha256_convenience(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        data = b"hello world"
        assert cp.get_sha256(data) == hashlib.sha256(data).hexdigest()


# ── FIPS mode fail-closed enforcement ─────────────────────────────────────────

class TestFIPSEnforcement:
    """Validate that M3_FIPS_MODE=1 enforces fail-closed policy."""

    def test_fips_mode_sets_backend_to_wolfssl(self, monkeypatch):
        """The module must select WOLFSSL when M3_FIPS_MODE=1."""
        # If wolfSSL is missing, the constructor raises — we catch that.
        try:
            cp = _reload_provider(monkeypatch, fips_mode="1")
            # If no exception: wolfSSL loaded successfully
            assert cp.BACKEND == "WOLFSSL"
        except RuntimeError as exc:
            # Fail-closed path: must mention FIPS in the message
            assert "FIPS" in str(exc) or "FATAL" in str(exc)

    def test_fips_mode_without_wolfssl_raises_on_sha256(self, monkeypatch):
        """Under M3_FIPS_MODE=1, sha256() must NOT silently use hashlib.

        Two valid outcomes depending on whether wolfssl.dll is installed:
          a) wolfSSL present: provider loads; we test boundary enforcement directly.
          b) wolfSSL absent: importlib.reload raises RuntimeError at module level
             (fail-closed enforcement — this IS the correct behavior).
        """
        monkeypatch.setenv("M3_FIPS_MODE", "1")
        import crypto_provider
        try:
            importlib.reload(crypto_provider)
        except RuntimeError as exc:
            # wolfSSL not installed — fail-closed raised during module init. Correct.
            assert "FIPS" in str(exc) or "FATAL" in str(exc)
            return

        # wolfSSL loaded — now test that boundary is enforced on a stub provider
        prov = crypto_provider.CryptoProvider.__new__(crypto_provider.CryptoProvider)
        prov.backend = "DEFAULT"
        prov._initialized = False
        prov._libwolf = None
        prov._fips_cb_ref = None

        with pytest.raises(RuntimeError, match="FIPS boundary violation"):
            prov.sha256(b"test")

    def test_fips_mode_without_wolfssl_raises_on_encrypt(self, monkeypatch):
        """Under M3_FIPS_MODE=1, encrypt() must NOT silently use stdlib."""
        monkeypatch.setenv("M3_FIPS_MODE", "1")
        import crypto_provider
        try:
            importlib.reload(crypto_provider)
        except RuntimeError as exc:
            assert "FIPS" in str(exc) or "FATAL" in str(exc)
            return

        prov = crypto_provider.CryptoProvider.__new__(crypto_provider.CryptoProvider)
        prov.backend = "DEFAULT"
        prov._initialized = False
        prov._libwolf = None
        prov._fips_cb_ref = None

        key = os.urandom(32)
        with pytest.raises(RuntimeError, match="FIPS boundary violation"):
            prov.encrypt(b"data", key)

    def test_fips_mode_without_wolfssl_raises_on_decrypt(self, monkeypatch):
        """Under M3_FIPS_MODE=1, decrypt() must NOT silently use stdlib."""
        monkeypatch.setenv("M3_FIPS_MODE", "1")
        import crypto_provider
        try:
            importlib.reload(crypto_provider)
        except RuntimeError as exc:
            assert "FIPS" in str(exc) or "FATAL" in str(exc)
            return

        prov = crypto_provider.CryptoProvider.__new__(crypto_provider.CryptoProvider)
        prov.backend = "DEFAULT"
        prov._initialized = False
        prov._libwolf = None
        prov._fips_cb_ref = None

        token = b"\x00" * 29
        key = os.urandom(32)
        with pytest.raises(RuntimeError, match="FIPS boundary violation"):
            prov.decrypt(token, key)

    def test_non_fips_mode_allows_stdlib(self, monkeypatch):
        """Without M3_FIPS_MODE, sha256 must succeed using stdlib fallback."""
        cp = _reload_provider(monkeypatch, backend="DEFAULT", fips_mode=None)
        result = cp.provider.sha256(b"plaintext")
        assert result == hashlib.sha256(b"plaintext").hexdigest()


# ── TLS context ────────────────────────────────────────────────────────────────

class TestTLSContext:
    """The SSL context from the DEFAULT backend must enforce TLS 1.3."""

    def test_default_ssl_context_tls13_minimum(self, monkeypatch):
        import ssl
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        ctx = cp.provider.get_ssl_context()
        assert isinstance(ctx, ssl.SSLContext)
        # TLS 1.3 minimum version (Python 3.7+ with openssl 1.1.1+)
        if hasattr(ssl, "TLSVersion"):
            assert ctx.minimum_version >= ssl.TLSVersion.TLSv1_3


# ── Error code table ───────────────────────────────────────────────────────────

class TestErrorCodeTable:
    def test_known_fips_error_codes_present(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        assert -203 in cp.WOLF_ERROR_CODES  # FIPS Integrity check failed
        assert -204 in cp.WOLF_ERROR_CODES  # FIPS state in error
