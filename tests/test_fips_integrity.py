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

def _reload_provider(monkeypatch, backend=None, fips_mode=None, strict=None):
    """Reload crypto_provider with specific env knobs."""
    if backend is not None:
        monkeypatch.setenv("M3_CRYPTO_BACKEND", backend)
    else:
        monkeypatch.delenv("M3_CRYPTO_BACKEND", raising=False)
    if fips_mode is not None:
        monkeypatch.setenv("M3_FIPS_MODE", fips_mode)
    else:
        monkeypatch.delenv("M3_FIPS_MODE", raising=False)
    if strict is not None:
        monkeypatch.setenv("M3_FIPS_STRICT", strict)
    else:
        monkeypatch.delenv("M3_FIPS_STRICT", raising=False)

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


# ── Power-up Known-Answer-Tests (Phase 2) ──────────────────────────────────────

def _load_mock_lib_wolf():
    """Load MockLibWolf from bin/test_fips_integrity.py by PATH.

    `from test_fips_integrity import MockLibWolf` is ambiguous — this tests/ file
    has the same module name and shadows the bin/ one on sys.path. Load the bin/
    module explicitly under a distinct name to avoid the collision.
    """
    import importlib.util as _ilu
    bin_path = os.path.join(os.path.dirname(__file__), "..", "bin", "test_fips_integrity.py")
    spec = _ilu.spec_from_file_location("_bin_fips_mock", os.path.abspath(bin_path))
    mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.MockLibWolf


def _wolf_provider_with_mock(monkeypatch, *, fips_mode="1", strict=None,
                             break_sha=False, non_fips=False):
    """Build a WOLFSSL-backed CryptoProvider with the MockLibWolf installed,
    so the power-up KATs run against a (correct or deliberately broken) backend
    without a real wolfSSL library. ``non_fips=True`` strips the CMVP FIPS
    service symbols to simulate the open-source wolfSSL build.

    Order matters: ctypes.CDLL must be patched BEFORE crypto_provider reloads,
    because the module instantiates `provider = CryptoProvider(BACKEND)` at
    import time — that init would otherwise try (and fail) to load the real
    wolfssl.dll before any per-test patch lands.
    """
    import ctypes as _ct

    MockLibWolf = _load_mock_lib_wolf()  # the bin/ mock (real-crypto-backed)

    mock = MockLibWolf()
    if break_sha:
        def _bad_sha(in_buf, sz, out_buf):
            _ct.memmove(out_buf, b"\x00" * 32, 32)  # wrong digest
            return 0
        mock.wc_Sha256Hash.func = _bad_sha
    if non_fips:
        # Simulate the OPEN-SOURCE wolfSSL build: it has the core crypto symbols
        # but NOT the CMVP FIPS service symbols (POST/entropy callbacks).
        for sym in ("wolfCrypt_GetStatus_fips", "wc_SetSeed_Cb", "wolfCrypt_SetCb_fips"):
            if hasattr(mock, sym):
                delattr(mock, sym)

    real_cdll = _ct.CDLL

    def _mock_cdll(name, *a, **k):
        if "wolfssl" in str(name).lower():
            return mock
        return real_cdll(name, *a, **k)

    # The secure loader resolves a TRUSTED ABSOLUTE path and only loads if the
    # file exists (it never loads a bare name). Create a real placeholder file
    # and pin it via M3_WOLFSSL_LIB so _resolve_wolfssl_path() returns it; the
    # CDLL monkeypatch then returns the mock instead of loading the placeholder.
    import tempfile
    fake = os.path.join(tempfile.mkdtemp(prefix="m3wolf-"), "wolfssl.dll")
    with open(fake, "wb") as f:
        f.write(b"mock-wolfssl-placeholder")
    monkeypatch.setenv("M3_WOLFSSL_LIB", fake)

    # Patch the shared ctypes module FIRST, then reload the provider so its
    # module-level `provider = CryptoProvider(BACKEND)` sees the mock.
    monkeypatch.setattr(_ct, "CDLL", _mock_cdll)
    cp = _reload_provider(monkeypatch, backend="WOLFSSL", fips_mode=fips_mode, strict=strict)
    return cp


class TestPowerUpKATs:
    def test_kats_pass_against_correct_backend(self, monkeypatch):
        cp = _wolf_provider_with_mock(monkeypatch, fips_mode="1")
        p = cp.CryptoProvider("WOLFSSL")
        assert p._initialized is True
        assert p.backend == "WOLFSSL"
        # Explicit re-run is idempotent and must not raise.
        p._run_self_tests()

    def test_kat_vectors_are_canonical(self, monkeypatch):
        """Lock the published KAT constants so a future edit can't silently
        weaken them (SHA-256('abc') is the NIST FIPS 180-4 example)."""
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        assert cp._KAT_SHA256_DIGEST == (
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        )
        assert cp._KAT_SHA256_INPUT == b"abc"
        assert len(bytes.fromhex(cp._KAT_PBKDF2_KEY_HEX)) == 32
        assert len(cp._KAT_AESGCM_KEY) == 32
        assert len(cp._KAT_AESGCM_NONCE) == 12

    def test_broken_backend_fatally_aborts_under_fips(self, monkeypatch):
        """A primitive that computes the WRONG answer must abort init under
        M3_FIPS_MODE=1 — the FIPS power-up-self-test failure contract. With the
        broken mock installed before reload, the module-level
        `provider = CryptoProvider(BACKEND)` raises during reload itself; a
        direct construction raises identically. Assert either path raises."""
        with pytest.raises(RuntimeError):
            cp = _wolf_provider_with_mock(monkeypatch, fips_mode="1", break_sha=True)
            cp.CryptoProvider("WOLFSSL")

    def test_self_tests_method_exists(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        assert hasattr(cp.CryptoProvider, "_run_self_tests")


# ── Tiered FIPS mode: M3_FIPS_MODE vs M3_FIPS_STRICT (Phase 4) ──────────────────

class TestTieredFipsMode:
    """M3_FIPS_MODE accepts the open-source (non-FIPS) wolfCrypt build for
    homelab/dev use; M3_FIPS_STRICT additionally requires the CMVP-validated
    FIPS module and refuses a non-FIPS lib with a clear message."""

    def test_fips_mode_accepts_non_fips_wolfssl(self, monkeypatch):
        """Homelab tier: M3_FIPS_MODE=1 + open-source wolfSSL (no FIPS symbols)
        -> initializes, runs real wolfCrypt, but flags itself non-validated."""
        cp = _wolf_provider_with_mock(monkeypatch, fips_mode="1", non_fips=True)
        p = cp.CryptoProvider("WOLFSSL")
        assert p._initialized is True
        assert p.backend == "WOLFSSL"
        assert p._fips_validated is False  # open-source build is NOT validated
        # Real wolfCrypt crypto still works.
        import hashlib
        assert p.sha256(b"abc") == hashlib.sha256(b"abc").hexdigest()

    def test_fips_strict_refuses_non_fips_wolfssl(self, monkeypatch):
        """Strict tier: M3_FIPS_STRICT=1 + open-source wolfSSL -> FATAL, with a
        message naming the open-source-vs-validated distinction."""
        with pytest.raises(RuntimeError) as exc:
            cp = _wolf_provider_with_mock(
                monkeypatch, fips_mode="1", strict="1", non_fips=True
            )
            cp.CryptoProvider("WOLFSSL")
        assert "OPEN-SOURCE" in str(exc.value) or "validated" in str(exc.value).lower()

    def test_fips_strict_accepts_validated_wolfssl(self, monkeypatch):
        """Strict tier with the FIPS symbols present (mock simulates validated)
        -> initializes and reports validated."""
        cp = _wolf_provider_with_mock(monkeypatch, fips_mode="1", strict="1", non_fips=False)
        p = cp.CryptoProvider("WOLFSSL")
        assert p._initialized is True
        assert p._fips_validated is True

    def test_fips_validated_flag_exists(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        p = cp.CryptoProvider("DEFAULT")
        assert hasattr(p, "_fips_validated")
        assert p._fips_validated is False  # DEFAULT backend is never validated


# ── Secure wolfSSL discovery: DLL-hijack hardening (Phase 5) ────────────────────

class TestSecureWolfsslDiscovery:
    """The crypto lib must load ONLY from trusted absolute paths M3 controls —
    never a bare name (which would let the OS search the CWD / %PATH% /
    LD_LIBRARY_PATH and enable a weaker-DLL injection)."""

    def test_candidates_are_all_absolute(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        for c in cp._trusted_wolfssl_candidates():
            assert os.path.isabs(c), f"non-absolute candidate (search-order risk): {c}"

    def test_macos_checks_dylib_in_m3_lib(self, monkeypatch):
        """macOS regression: the build helper installs libwolfssl.dylib to
        ~/.m3/lib, so the loader MUST check .dylib there (not only .so)."""
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        monkeypatch.setattr(cp.os, "name", "posix")
        monkeypatch.setattr(cp.sys, "platform", "darwin")
        m3lib = cp._m3_lib_dir()
        cands = cp._trusted_wolfssl_candidates()
        assert os.path.join(m3lib, "libwolfssl.dylib") in cands
        assert os.path.join(m3lib, "libwolfssl.so") in cands  # also accept .so

    def test_windows_uses_dll(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        monkeypatch.setattr(cp.os, "name", "nt")
        cands = cp._trusted_wolfssl_candidates()
        assert all(c.endswith("wolfssl.dll") for c in cands), cands

    def test_linux_uses_so_only(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        monkeypatch.setattr(cp.os, "name", "posix")
        monkeypatch.setattr(cp.sys, "platform", "linux")
        cands = cp._trusted_wolfssl_candidates()
        assert all(c.endswith("libwolfssl.so") for c in cands), cands

    def test_candidates_exclude_cwd_and_path(self, monkeypatch):
        """The trusted list must not include the current dir or PATH entries."""
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        cands = cp._trusted_wolfssl_candidates()
        cwd = os.path.abspath(os.getcwd())
        for c in cands:
            assert os.path.dirname(os.path.abspath(c)) != cwd, "CWD is a hijack vector"
        # No candidate should come from a %PATH%/LD_LIBRARY_PATH dir.
        path_dirs = (os.environ.get("PATH", "") + os.pathsep +
                     os.environ.get("LD_LIBRARY_PATH", "")).split(os.pathsep)
        path_dirs = {os.path.abspath(d) for d in path_dirs if d}
        for c in cands:
            assert os.path.dirname(os.path.abspath(c)) not in path_dirs

    def test_explicit_pin_takes_precedence(self, monkeypatch, tmp_path):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        pinned = tmp_path / "wolfssl.dll"
        pinned.write_bytes(b"x")
        monkeypatch.setenv("M3_WOLFSSL_LIB", str(pinned))
        assert cp._trusted_wolfssl_candidates()[0] == os.path.abspath(str(pinned))
        assert cp._resolve_wolfssl_path() == str(pinned)

    def test_resolve_returns_none_when_no_trusted_lib(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        monkeypatch.delenv("M3_WOLFSSL_LIB", raising=False)
        # Point M3 roots at an empty temp area so ~/.m3/lib resolves nowhere real.
        import tempfile
        monkeypatch.setenv("M3_MEMORY_ROOT", tempfile.mkdtemp(prefix="m3empty-"))
        # System dirs won't have wolfssl on a test box; expect None.
        assert cp._resolve_wolfssl_path() is None

    def test_integrity_pin_rejects_mismatch(self, monkeypatch, tmp_path):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        lib = tmp_path / "wolfssl.dll"
        lib.write_bytes(b"the-real-bytes")
        monkeypatch.setenv("M3_WOLFSSL_LIB", str(lib))
        monkeypatch.setenv("M3_WOLFSSL_SHA256", "00" * 32)  # wrong
        with pytest.raises(RuntimeError, match="integrity pin"):
            cp._resolve_wolfssl_path()

    def test_integrity_pin_accepts_match(self, monkeypatch, tmp_path):
        import hashlib
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        lib = tmp_path / "wolfssl.dll"
        data = b"the-real-bytes"
        lib.write_bytes(data)
        monkeypatch.setenv("M3_WOLFSSL_LIB", str(lib))
        monkeypatch.setenv("M3_WOLFSSL_SHA256", hashlib.sha256(data).hexdigest())
        assert cp._resolve_wolfssl_path() == str(lib)

    def test_fips_mode_fatal_when_no_trusted_lib(self, monkeypatch):
        """FIPS mode + no wolfSSL in any trusted path -> FATAL (never silently
        falls back, never searches CWD/PATH)."""
        import tempfile
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        monkeypatch.delenv("M3_WOLFSSL_LIB", raising=False)
        monkeypatch.setenv("M3_MEMORY_ROOT", tempfile.mkdtemp(prefix="m3empty-"))
        monkeypatch.setenv("M3_FIPS_MODE", "1")
        with pytest.raises(RuntimeError, match="TRUSTED|trusted"):
            cp.CryptoProvider("WOLFSSL")
