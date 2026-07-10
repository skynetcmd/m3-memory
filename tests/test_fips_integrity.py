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
import json
import os
import subprocess
import sys
import textwrap

import pytest

_BIN = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "bin"))
sys.path.insert(0, _BIN)


# ── Subprocess isolation for native-wolfSSL tests (#85) ─────────────────────────
#
# Loading crypto_provider under a wolfSSL backend initializes the native wolfSSL
# library via ctypes at import time (`provider = CryptoProvider(BACKEND)`). Once
# that DLL is loaded, any later importlib.reload() of crypto_provider re-inits it
# in the same process and, on Windows, segfaults the ENTIRE pytest run (Windows
# fatal exception: access violation, #85). A fresh interpreter loads it exactly
# once and exits cleanly, so every test that must load wolfSSL (real or the
# ctypes-mocked build) runs its assertions in a subprocess and we inspect the
# JSON verdict here. DEFAULT-backend tests don't touch native code and stay
# in-process. This mirrors conftest's native-embedder-disabled discipline.

def _run_fips_subprocess(body: str, env: dict) -> dict:
    """Execute `body` in a fresh interpreter with `env` overlaid; return the dict
    it prints via `_emit(**kv)`. `_install_mock(...)` and `_bin_mock()` are
    available to the snippet for the ctypes-mocked wolfSSL build. A crash or a
    missing verdict surfaces as a readable test failure — never a crash of the
    shared test run (that's the whole point)."""
    prelude = textwrap.dedent(f"""
        import ctypes, hashlib, importlib, json, os, sys, tempfile
        sys.path.insert(0, {_BIN!r})

        def _emit(**kv):
            print("__FIPS__" + json.dumps(kv))

        def _bin_mock():
            # Load MockLibWolf from bin/test_fips_integrity.py by path (the tests/
            # file of the same name shadows it on sys.path).
            import importlib.util as _ilu
            p = os.path.join({_BIN!r}, "test_fips_integrity.py")
            spec = _ilu.spec_from_file_location("_bin_fips_mock", p)
            m = _ilu.module_from_spec(spec); spec.loader.exec_module(m)
            return m.MockLibWolf

        def _install_mock(*, break_sha=False, non_fips=False):
            # Patch ctypes.CDLL to return a MockLibWolf for wolfssl, pin a real
            # placeholder file so the secure loader resolves it, then return the
            # freshly-imported crypto_provider. Mirrors the in-process helper.
            MockLibWolf = _bin_mock()
            mock = MockLibWolf()
            if break_sha:
                def _bad_sha(in_buf, sz, out_buf):
                    ctypes.memmove(out_buf, b"\\x00" * 32, 32); return 0
                mock.wc_Sha256Hash.func = _bad_sha
            if non_fips:
                for sym in ("wolfCrypt_GetStatus_fips", "wc_SetSeed_Cb", "wolfCrypt_SetCb_fips"):
                    if hasattr(mock, sym):
                        delattr(mock, sym)
            real_cdll = ctypes.CDLL
            def _mock_cdll(name, *a, **k):
                if "wolfssl" in str(name).lower():
                    return mock
                return real_cdll(name, *a, **k)
            fake = os.path.join(tempfile.mkdtemp(prefix="m3wolf-"), "wolfssl.dll")
            with open(fake, "wb") as f:
                f.write(b"mock-wolfssl-placeholder")
            os.environ["M3_WOLFSSL_LIB"] = fake
            ctypes.CDLL = _mock_cdll
            import crypto_provider
            return crypto_provider
    """)
    proc = subprocess.run(
        [sys.executable, "-c", prelude + textwrap.dedent(body)],
        capture_output=True, text=True,
        env={**os.environ, **{k: str(v) for k, v in env.items()}},
        timeout=120,
    )
    marker = "__FIPS__"
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith(marker)), None)
    assert line is not None, (
        f"FIPS subprocess produced no verdict (rc={proc.returncode}).\n"
        f"stdout:\n{proc.stdout[-3000:]}\nstderr:\n{proc.stderr[-2000:]}"
    )
    return json.loads(line[len(marker):])


# ── Helpers ────────────────────────────────────────────────────────────────────

def _reload_provider(monkeypatch, backend=None, fips_mode=None, strict=None):
    """Reload crypto_provider with specific env knobs (DEFAULT backend only — the
    native-wolfSSL path is exercised via _run_fips_subprocess to avoid the #85
    in-process reload segfault)."""
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

    def test_fips_mode_sets_backend_to_wolfssl(self):
        """The module must select WOLFSSL when M3_FIPS_MODE=1. Isolated (#85):
        either wolfSSL loads (BACKEND==WOLFSSL) or it is absent and the module
        fails closed with a RuntimeError mentioning FIPS — both accepted."""
        v = _run_fips_subprocess(
            """
            try:
                import crypto_provider
                _emit(outcome="loaded", backend=crypto_provider.BACKEND)
            except RuntimeError as exc:
                _emit(outcome="raised", msg=str(exc))
            """,
            env={"M3_FIPS_MODE": "1"},
        )
        if v["outcome"] == "loaded":
            assert v["backend"] == "WOLFSSL"
        else:
            assert "FIPS" in v["msg"] or "FATAL" in v["msg"]

    def _assert_fips_boundary(self, primitive: str):
        """Under M3_FIPS_MODE=1 a primitive must NOT silently use stdlib: either
        the module fails closed at import (wolfSSL absent), or wolfSSL loads and a
        stub DEFAULT-backend provider raises 'FIPS boundary violation'. Isolated
        in a subprocess (#85)."""
        v = _run_fips_subprocess(
            f"""
            try:
                import crypto_provider
            except RuntimeError as exc:
                _emit(outcome="raised", msg=str(exc)); sys.exit(0)
            prov = crypto_provider.CryptoProvider.__new__(crypto_provider.CryptoProvider)
            prov.backend = "DEFAULT"; prov._initialized = False
            prov._libwolf = None; prov._fips_cb_ref = None
            try:
                if {primitive!r} == "sha256":
                    prov.sha256(b"test")
                elif {primitive!r} == "encrypt":
                    prov.encrypt(b"data", os.urandom(32))
                else:
                    prov.decrypt(b"\\x00" * 29, os.urandom(32))
                _emit(outcome="no_raise")
            except RuntimeError as exc:
                _emit(outcome="boundary", msg=str(exc))
            """,
            env={"M3_FIPS_MODE": "1"},
        )
        if v["outcome"] == "raised":
            assert "FIPS" in v["msg"] or "FATAL" in v["msg"]
        else:
            assert v["outcome"] == "boundary", (
                f"{primitive} must not silently use stdlib under FIPS mode"
            )
            assert "FIPS boundary violation" in v["msg"]

    def test_fips_mode_without_wolfssl_raises_on_sha256(self):
        """Under M3_FIPS_MODE=1, sha256() must NOT silently use hashlib."""
        self._assert_fips_boundary("sha256")

    def test_fips_mode_without_wolfssl_raises_on_encrypt(self):
        """Under M3_FIPS_MODE=1, encrypt() must NOT silently use stdlib."""
        self._assert_fips_boundary("encrypt")

    def test_fips_mode_without_wolfssl_raises_on_decrypt(self):
        """Under M3_FIPS_MODE=1, decrypt() must NOT silently use stdlib."""
        self._assert_fips_boundary("decrypt")

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
# The MockLibWolf install + reload that these used in-process now lives in the
# subprocess prelude's `_install_mock()` (see _run_fips_subprocess) — the ctypes
# CDLL patch + native provider init runs in a fresh process, so it can never
# corrupt the shared test run (#85).


class TestPowerUpKATs:
    def test_kats_pass_against_correct_backend(self):
        v = _run_fips_subprocess(
            """
            cp = _install_mock()
            p = cp.CryptoProvider("WOLFSSL")
            p._run_self_tests()  # idempotent re-run must not raise
            _emit(initialized=p._initialized, backend=p.backend)
            """,
            env={"M3_FIPS_MODE": "1"},
        )
        assert v["initialized"] is True
        assert v["backend"] == "WOLFSSL"

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

    def test_broken_backend_fatally_aborts_under_fips(self):
        """A primitive that computes the WRONG answer must abort init under
        M3_FIPS_MODE=1 — the FIPS power-up-self-test failure contract. With the
        broken mock installed before import, the module-level
        `provider = CryptoProvider(BACKEND)` raises during import itself; a direct
        construction raises identically. Assert either path raises."""
        v = _run_fips_subprocess(
            """
            raised = False
            try:
                cp = _install_mock(break_sha=True)
                cp.CryptoProvider("WOLFSSL")
            except RuntimeError:
                raised = True
            _emit(raised=raised)
            """,
            env={"M3_FIPS_MODE": "1"},
        )
        assert v["raised"] is True

    def test_self_tests_method_exists(self, monkeypatch):
        cp = _reload_provider(monkeypatch, backend="DEFAULT")
        assert hasattr(cp.CryptoProvider, "_run_self_tests")


# ── Tiered FIPS mode: M3_FIPS_MODE vs M3_FIPS_STRICT (Phase 4) ──────────────────

class TestTieredFipsMode:
    """M3_FIPS_MODE accepts the open-source (non-FIPS) wolfCrypt build for
    homelab/dev use; M3_FIPS_STRICT additionally requires the CMVP-validated
    FIPS module and refuses a non-FIPS lib with a clear message."""

    def test_fips_mode_accepts_non_fips_wolfssl(self):
        """Homelab tier: M3_FIPS_MODE=1 + open-source wolfSSL (no FIPS symbols)
        -> initializes, runs real wolfCrypt, but flags itself non-validated."""
        v = _run_fips_subprocess(
            """
            cp = _install_mock(non_fips=True)
            p = cp.CryptoProvider("WOLFSSL")
            _emit(initialized=p._initialized, backend=p.backend,
                  validated=p._fips_validated,
                  sha_ok=(p.sha256(b"abc") == hashlib.sha256(b"abc").hexdigest()))
            """,
            env={"M3_FIPS_MODE": "1"},
        )
        assert v["initialized"] is True
        assert v["backend"] == "WOLFSSL"
        assert v["validated"] is False  # open-source build is NOT validated
        assert v["sha_ok"] is True      # real wolfCrypt crypto still works

    def test_fips_strict_refuses_non_fips_wolfssl(self):
        """Strict tier: M3_FIPS_STRICT=1 + open-source wolfSSL -> FATAL, with a
        message naming the open-source-vs-validated distinction."""
        v = _run_fips_subprocess(
            """
            msg = None
            try:
                cp = _install_mock(non_fips=True)
                cp.CryptoProvider("WOLFSSL")
            except RuntimeError as exc:
                msg = str(exc)
            _emit(msg=msg)
            """,
            env={"M3_FIPS_MODE": "1", "M3_FIPS_STRICT": "1"},
        )
        assert v["msg"] is not None, "strict mode must refuse a non-FIPS build"
        assert "OPEN-SOURCE" in v["msg"] or "validated" in v["msg"].lower()

    def test_fips_strict_accepts_validated_wolfssl(self):
        """Strict tier with the FIPS symbols present (mock simulates validated)
        -> initializes and reports validated."""
        v = _run_fips_subprocess(
            """
            cp = _install_mock(non_fips=False)
            p = cp.CryptoProvider("WOLFSSL")
            _emit(initialized=p._initialized, validated=p._fips_validated)
            """,
            env={"M3_FIPS_MODE": "1", "M3_FIPS_STRICT": "1"},
        )
        assert v["initialized"] is True
        assert v["validated"] is True

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
        # BOTH roots must move: _m3_lib_dir() prefers M3_CONFIG_ROOT's parent over
        # M3_MEMORY_ROOT, so on a dev box that has a real ~/.m3/lib/wolfssl.dll,
        # redirecting only M3_MEMORY_ROOT leaves the real lib discoverable.
        import tempfile
        empty = tempfile.mkdtemp(prefix="m3empty-")
        monkeypatch.setenv("M3_MEMORY_ROOT", empty)
        monkeypatch.setenv("M3_CONFIG_ROOT", os.path.join(empty, "config"))
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

    def test_fips_mode_fatal_when_no_trusted_lib(self):
        """FIPS mode + no wolfSSL in any trusted path -> FATAL (never silently
        falls back, never searches CWD/PATH). Isolated (#85): constructing the
        WOLFSSL provider touches the native loader."""
        v = _run_fips_subprocess(
            """
            os.environ.pop("M3_WOLFSSL_LIB", None)
            # Move BOTH roots so a real ~/.m3/lib/wolfssl.dll on a dev box can't be
            # discovered (M3_CONFIG_ROOT's parent wins over M3_MEMORY_ROOT).
            empty = tempfile.mkdtemp(prefix="m3empty-")
            os.environ["M3_MEMORY_ROOT"] = empty
            os.environ["M3_CONFIG_ROOT"] = os.path.join(empty, "config")
            # Under FIPS mode with no trusted lib, fail-closed can fire either at
            # module import (the module-level provider init) OR at explicit
            # construction — accept a RuntimeError from either point.
            msg = None
            try:
                import crypto_provider as cp
                cp.CryptoProvider("WOLFSSL")
            except RuntimeError as exc:
                msg = str(exc)
            _emit(msg=msg)
            """,
            env={"M3_FIPS_MODE": "1"},
        )
        assert v["msg"] is not None, "FIPS mode with no trusted lib must be FATAL"
        assert "TRUSTED" in v["msg"] or "trusted" in v["msg"]
