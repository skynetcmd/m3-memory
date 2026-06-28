#!/usr/bin/env python3
"""
crypto_provider.py — Abstraction layer for FIPS-ready cryptography.
Supports standard Python crypto and wolfSSL/wolfCrypt backends.
"""

import ctypes
import hashlib
import logging
import os
import sys
from typing import Optional

logger = logging.getLogger("m3-crypto")

# FIPS 140-3 Error Code Mapping
WOLF_ERROR_CODES = {
    -197: "FIPS mode not allowed for this algorithm",
    -200: "HMAC key length too short for FIPS",
    -203: "FIPS Integrity check failed (In-Core Hash mismatch)",
    -204: "FIPS state is in error",
}

# ── Approved-algorithm whitelist (the FIPS algorithm boundary) ───────────────
# The ONLY cryptographic primitives M3 uses for security. This is the
# authoritative list; the boundary is enforced two ways:
#   1. crypto_provider exposes only these as services (sha256, AES-256-GCM,
#      PBKDF2-HMAC-SHA256, TLS 1.3) — there's no API to ask it for a weak algo.
#   2. tests/test_fips_algorithm_whitelist.py statically scans bin/ + m3_memory/
#      and FAILS if any non-approved primitive (MD5, SHA-1-for-security, DES,
#      RC4, new Fernet) is used directly — Python imports can't be sandboxed, so
#      enforcement-by-test is the mechanism. Non-security hashes must pass
#      `usedforsecurity=False`; legacy Fernet is decrypt-only (migration).
FIPS_APPROVED_ALGORITHMS = frozenset({
    "AES-256-GCM",          # authenticated encryption (vault)
    "SHA-256", "SHA-384", "SHA-512",
    "HMAC-SHA-256",
    "PBKDF2-HMAC-SHA256",   # key derivation
    "CTR-DRBG",             # via the validated module when present
    "TLS1.3",               # transport
})

# Primitives that are NEVER approved for security use anywhere in M3.
FIPS_BLOCKED_ALGORITHMS = frozenset({
    "MD5", "MD4", "MD2",
    "SHA-1",                # allowed ONLY with usedforsecurity=False (non-crypto)
    "DES", "3DES", "TripleDES", "RC4", "ARC4", "Blowfish", "RC2",
    "Fernet",               # AES-128-CBC; legacy decrypt-only, never new writes
})

# ── FIPS 140-3 power-up Known-Answer-Test (KAT) vectors ──────────────────────
# In-process self-tests run at provider init (wolfSSL backend) to prove each
# approved primitive computes the documented answer before any real crypto is
# served. Vectors are canonical:
#   - SHA-256: NIST FIPS 180-4 example, SHA256("abc").
#   - PBKDF2-SHA256: RFC 7914 §11 (passwd / salt / 1 iter / 32-byte dk).
#   - AES-256-GCM: deterministic encrypt with a fixed key+nonce (round-trip +
#     known ciphertext||tag). The tag also exercises GCM authentication.
# A KAT mismatch under M3_FIPS_MODE is fatal — the module enters an error state
# and refuses to serve (FIPS 140-3 requirement).
_KAT_SHA256_INPUT = b"abc"
_KAT_SHA256_DIGEST = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

_KAT_PBKDF2_PASSWORD = b"passwd"
_KAT_PBKDF2_SALT = b"salt"
_KAT_PBKDF2_ITERATIONS = 1
_KAT_PBKDF2_DKLEN = 32
_KAT_PBKDF2_KEY_HEX = "55ac046e56e3089fec1691c22544b605f94185216dde0465e68b9d57c20dacbc"

_KAT_AESGCM_KEY = bytes(range(32))            # 256-bit key 00..1f
_KAT_AESGCM_NONCE = bytes(range(12))          # 96-bit nonce 00..0b
_KAT_AESGCM_PLAINTEXT = b"FIPS-KAT-AESGCM!"   # 16 bytes
_KAT_AESGCM_CT_TAG_HEX = "014b8648e8ae834fa000d2d8f6aa354c6279c93ba1d84effc8a3bae200a581a8"

# ── Tiered FIPS mode (see docs/FIPS_MODULE_BOUNDARY.md) ──────────────────────
# Three tiers so homelab/dev users can use wolfCrypt's performance + the
# hardened algorithm boundary WITHOUT a commercial FIPS license:
#
#   (neither set)            -> DEFAULT backend (Python cryptography/hashlib).
#   M3_FIPS_MODE=1           -> require the wolfSSL backend; FAIL-CLOSED if
#                               wolfSSL is ABSENT; but ACCEPT the open-source
#                               (non-FIPS) wolfCrypt build. The CMVP FIPS
#                               symbols (POST/entropy callbacks) are OPTIONAL
#                               here — missing them is a warning, not fatal.
#   M3_FIPS_MODE=1 +
#   M3_FIPS_STRICT=1         -> additionally REQUIRE the CMVP-validated FIPS
#                               build: FIPS symbols mandatory, POST must pass.
#                               A non-FIPS lib is refused with a clear message.
#
# Rationale: the validated wolfCrypt-FIPS module is commercial + NDA-gated and
# not freely downloadable; the open-source wolfSSL build is freely buildable but
# lacks the FIPS symbols. STRICT is the gate for true FIPS 140-3; plain FIPS_MODE
# is "use wolfCrypt, hardened, fail-closed-if-absent" for everyone else.
def _fips_mode() -> bool:
    return os.environ.get("M3_FIPS_MODE") == "1"


def _fips_strict() -> bool:
    # STRICT implies MODE; setting STRICT alone also engages the wolfSSL backend.
    return os.environ.get("M3_FIPS_STRICT") == "1"


# ── Secure wolfSSL discovery (DLL-hijack / search-order hardening) ───────────
# A bare `ctypes.CDLL("wolfssl.dll")` delegates to the OS loader search order,
# which on Windows includes the CWD and %PATH%, and on Linux honours the
# attacker-influenceable LD_LIBRARY_PATH. For a CRYPTO library that is a
# privilege-to-weaken vector: drop a malicious wolfssl.dll earlier in the search
# path and you replace the crypto module. We therefore NEVER load by bare name.
# Instead we resolve to an ABSOLUTE path from an explicit, trusted precedence
# list that M3 controls, and load exactly that file.
#
# Precedence (first existing file wins):
#   1. M3_WOLFSSL_LIB env var — an explicit absolute path the operator pins.
#   2. M3's own lib dir: <config-root-parent>/.m3/lib (default ~/.m3/lib).
#   3. Trusted SYSTEM install locations (NOT the CWD, NOT %PATH%):
#        Linux/macOS: /usr/local/lib, /usr/lib, /lib, /opt/homebrew/lib
#        Windows:     %SystemRoot%\System32  (admin-only writable)
# An operator may additionally pin the expected SHA-256 via M3_WOLFSSL_SHA256;
# a mismatch is fatal (catches an in-place swap even at a trusted path).
def _m3_lib_dir() -> str:
    """M3's own trusted lib directory (~/.m3/lib by default).

    Mirrors the decoupled-roots resolution without importing m3_sdk (the crypto
    layer stays dependency-light): M3_CONFIG_ROOT's parent, else
    M3_MEMORY_ROOT/.. , else ~/.m3 — then /lib.
    """
    cfg = os.environ.get("M3_CONFIG_ROOT")
    if cfg:
        base = os.path.dirname(os.path.abspath(os.path.expanduser(cfg)))
    else:
        mem = os.environ.get("M3_MEMORY_ROOT")
        base = (os.path.abspath(os.path.expanduser(mem)) if mem
                else os.path.join(os.path.expanduser("~"), ".m3"))
    return os.path.join(base, "lib")


def _trusted_wolfssl_candidates() -> "list[str]":
    """Ordered list of ABSOLUTE candidate paths for the wolfSSL library.

    Only locations M3 controls or that require elevated privileges to write —
    deliberately excludes the CWD and PATH/LD_LIBRARY_PATH entries.
    """
    # Per-OS library filename(s). macOS uses .dylib (what the build helper
    # produces and installs); we ALSO accept .so there since a Linux-style build
    # or a Homebrew formula may produce it. Windows: wolfssl.dll.
    if os.name == "nt":
        lib_names = ("wolfssl.dll",)
    elif sys.platform == "darwin":
        lib_names = ("libwolfssl.dylib", "libwolfssl.so")
    else:
        lib_names = ("libwolfssl.so",)

    candidates: "list[str]" = []
    # 1. Explicit operator pin (absolute path) — used verbatim, any filename.
    pinned = os.environ.get("M3_WOLFSSL_LIB", "").strip()
    if pinned:
        candidates.append(os.path.abspath(os.path.expanduser(pinned)))
    # 2. M3's own lib dir — try every per-OS filename.
    m3lib = _m3_lib_dir()
    for nm in lib_names:
        candidates.append(os.path.join(m3lib, nm))
    # 3. Trusted system locations (admin/root-only writable).
    if os.name == "nt":
        sysroot = os.environ.get("SystemRoot", r"C:\Windows")
        candidates.append(os.path.join(sysroot, "System32", "wolfssl.dll"))
    else:
        for d in ("/usr/local/lib", "/usr/lib", "/lib", "/opt/homebrew/lib"):
            for nm in lib_names:
                candidates.append(os.path.join(d, nm))
    return candidates


def _verify_lib_integrity(path: str) -> None:
    """If M3_WOLFSSL_SHA256 is set, the loaded file's SHA-256 must match it.

    Self-pin / trust-on-first-use: M3 doesn't bundle wolfSSL, so the user BUILDS
    their own and pins the hash of THAT trusted build (there is no canonical
    vendor hash to compare against). This then detects any later tampering or
    in-place swap. Raises RuntimeError on mismatch. No-op when unset."""
    expected = os.environ.get("M3_WOLFSSL_SHA256", "").strip().lower()
    if not expected:
        return
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected:
        raise RuntimeError(
            f"FATAL: wolfSSL library at {path} failed integrity pin "
            f"(M3_WOLFSSL_SHA256): expected {expected[:16]}…, got {actual[:16]}…. "
            f"Refusing to load a crypto library that does not match the pinned hash."
        )


def _resolve_wolfssl_path() -> "Optional[str]":
    """Return the FIRST trusted candidate that exists on disk (absolute path),
    after verifying any integrity pin. None if none found."""
    for cand in _trusted_wolfssl_candidates():
        if os.path.isfile(cand):
            _verify_lib_integrity(cand)
            return cand
    return None


# Backend Selection
# DEFAULT: standard hashlib/cryptography
# WOLFSSL: wolfCrypt (FIPS-Ready)
BACKEND = os.environ.get("M3_CRYPTO_BACKEND", "DEFAULT").upper()
if _fips_mode() or _fips_strict():
    BACKEND = "WOLFSSL"

class CryptoProvider:
    def __init__(self, backend="DEFAULT"):
        self.backend = backend
        self._initialized = False
        self._fips_validated = False  # True only when the CMVP FIPS module is active
        self._libwolf: Optional[ctypes.CDLL] = None
        self._lib_path: Optional[str] = None  # absolute path the lib was loaded from
        self._fips_cb_ref = None  # Keep reference to prevent GC

        if self.backend == "WOLFSSL":
            self._initialize_wolfssl()

        # Fail-closed lockouts.
        # (a) Any FIPS tier requires the wolfSSL backend to be live — if wolfSSL
        #     is absent/failed, never silently fall back to Python crypto.
        if (_fips_mode() or _fips_strict()) and (not self._initialized or self.backend != "WOLFSSL"):
            raise RuntimeError(
                "FATAL: FIPS mode enabled (M3_FIPS_MODE/M3_FIPS_STRICT) but the "
                "wolfSSL/wolfCrypt backend failed to initialize. Terminating to "
                "prevent unsafe cryptographic fallback. Install wolfSSL (see "
                "docs/FIPS_MODULE_BOUNDARY.md), or unset the FIPS env vars."
            )
        # (b) STRICT additionally requires the CMVP-VALIDATED module — refuse a
        #     non-FIPS (open-source) wolfSSL with a clear, actionable message.
        if _fips_strict() and not self._fips_validated:
            raise RuntimeError(
                "FATAL: M3_FIPS_STRICT=1 requires the CMVP-validated wolfCrypt "
                "FIPS module, but the loaded wolfSSL library lacks the FIPS "
                "symbols (POST/entropy) — you have the OPEN-SOURCE build, not the "
                "validated FIPS module. Use the commercial wolfSSL FIPS build for "
                "strict compliance, or use M3_FIPS_MODE=1 (without STRICT) to run "
                "on open-source wolfCrypt. See docs/FIPS_MODULE_BOUNDARY.md."
            )

    def _initialize_wolfssl(self):
        """Foundational FIPS 140-3 initialization sequence."""
        try:
            # SECURE LOAD: resolve to a trusted ABSOLUTE path (M3-controlled or
            # admin-only locations) and load exactly that file. We never pass a
            # bare "wolfssl.dll"/"libwolfssl.so" to ctypes.CDLL, because that
            # delegates to the OS loader search order (Windows: app dir + CWD +
            # %PATH%; Linux: LD_LIBRARY_PATH + runpath) — a DLL-hijack vector
            # where an attacker drops a weaker/backdoored crypto lib earlier in
            # the path. See _resolve_wolfssl_path / _trusted_wolfssl_candidates.
            self._lib_path = _resolve_wolfssl_path()
            if self._lib_path is None:
                if _fips_mode() or _fips_strict():
                    raise RuntimeError(
                        "FATAL: FIPS mode enabled but no wolfSSL library was found "
                        "in any TRUSTED location (M3_WOLFSSL_LIB, ~/.m3/lib, or a "
                        "system lib dir). M3 refuses to search the CWD/PATH for a "
                        "crypto library. Install wolfSSL to a trusted path or unset "
                        "M3_FIPS_MODE/M3_FIPS_STRICT (see docs/FIPS_MODULE_BOUNDARY.md)."
                    )
                logger.info("M3 Crypto: no wolfSSL in a trusted path; using DEFAULT backend.")
                self.backend = "DEFAULT"
                return
            try:
                # winmode=0 on Windows prevents the default flags from re-adding
                # the application/PATH search directories around the absolute path.
                if os.name == "nt":
                    self._libwolf = ctypes.CDLL(self._lib_path, winmode=0)
                else:
                    self._libwolf = ctypes.CDLL(self._lib_path)
                logger.info(f"M3 Crypto: loaded wolfSSL from trusted path {self._lib_path}")
            except OSError as e:
                if _fips_mode() or _fips_strict():
                    raise RuntimeError(
                        f"FATAL: FIPS mode enabled but the wolfSSL library at the "
                        f"trusted path {self._lib_path} could not be loaded: {e}."
                    )
                logger.info(f"M3 Crypto: wolfSSL at {self._lib_path} failed to load ({e}); using DEFAULT.")
                self.backend = "DEFAULT"
                return

            # Setup ctypes function signatures if library loaded successfully
            if hasattr(self._libwolf, "wc_AesGcmSetKey"):
                self._libwolf.wc_AesGcmSetKey.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint]
                self._libwolf.wc_AesGcmSetKey.restype = ctypes.c_int
            elif (_fips_mode() or _fips_strict()):
                raise RuntimeError("FATAL: wc_AesGcmSetKey symbol not found in loaded wolfSSL library.")

            if hasattr(self._libwolf, "wc_AesGcmEncrypt"):
                self._libwolf.wc_AesGcmEncrypt.argtypes = [
                    ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint,
                    ctypes.c_char_p, ctypes.c_uint, ctypes.c_char_p, ctypes.c_uint,
                    ctypes.c_char_p, ctypes.c_uint
                ]
                self._libwolf.wc_AesGcmEncrypt.restype = ctypes.c_int
            elif (_fips_mode() or _fips_strict()):
                raise RuntimeError("FATAL: wc_AesGcmEncrypt symbol not found in loaded wolfSSL library.")

            if hasattr(self._libwolf, "wc_AesGcmDecrypt"):
                self._libwolf.wc_AesGcmDecrypt.argtypes = [
                    ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint,
                    ctypes.c_char_p, ctypes.c_uint, ctypes.c_char_p, ctypes.c_uint,
                    ctypes.c_char_p, ctypes.c_uint
                ]
                self._libwolf.wc_AesGcmDecrypt.restype = ctypes.c_int
            elif (_fips_mode() or _fips_strict()):
                raise RuntimeError("FATAL: wc_AesGcmDecrypt symbol not found in loaded wolfSSL library.")

            if hasattr(self._libwolf, "wc_Sha256Hash"):
                self._libwolf.wc_Sha256Hash.argtypes = [ctypes.c_char_p, ctypes.c_uint, ctypes.c_char_p]
                self._libwolf.wc_Sha256Hash.restype = ctypes.c_int
            elif (_fips_mode() or _fips_strict()):
                raise RuntimeError("FATAL: wc_Sha256Hash symbol not found in loaded wolfSSL library.")

            if hasattr(self._libwolf, "wc_PBKDF2"):
                # wc_PBKDF2(out, passwd, pLen, salt, sLen, iterations, kLen, typeH)
                self._libwolf.wc_PBKDF2.argtypes = [
                    ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int,
                    ctypes.c_char_p, ctypes.c_int, ctypes.c_int,
                    ctypes.c_int, ctypes.c_int,
                ]
                self._libwolf.wc_PBKDF2.restype = ctypes.c_int
            elif (_fips_mode() or _fips_strict()):
                raise RuntimeError("FATAL: wc_PBKDF2 symbol not found in loaded wolfSSL library.")

            # 1. Register FIPS Status Callback
            # wolfCrypt_SetCb_fips(myFipsCb)
            FIPS_CB_TYPE = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_int, ctypes.c_char_p)

            def myFipsCb(status, msg):
                msg_str = msg.decode('utf-8') if msg else "No message"
                err_desc = WOLF_ERROR_CODES.get(status, "Status OK" if status == 0 else "Unknown Status")
                logger.info(f"FIPS Status: {status} ({err_desc}) - {msg_str}")
                return 0

            self._fips_cb_ref = FIPS_CB_TYPE(myFipsCb)
            if hasattr(self._libwolf, "wolfCrypt_SetCb_fips"):
                self._libwolf.wolfCrypt_SetCb_fips(self._fips_cb_ref)

            # 2. Register Entropy Seed Callback (CMVP FIPS module only).
            # wc_SetSeed_Cb(wc_GenerateSeed)
            has_entropy_cb = (
                hasattr(self._libwolf, "wc_SetSeed_Cb")
                and hasattr(self._libwolf, "wc_GenerateSeed")
            )
            if has_entropy_cb:
                self._libwolf.wc_SetSeed_Cb(self._libwolf.wc_GenerateSeed)
                logger.info("M3 Crypto: Entropy seed callback registered.")

            # 3. Verify POST (Pre-Operational Self-Tests) — CMVP FIPS module only.
            # wolfCrypt_GetStatus_fips() returns 0 for success. The open-source
            # wolfSSL build lacks this symbol entirely (that's what makes it
            # "non-FIPS"); its absence is NOT fatal under plain M3_FIPS_MODE, but
            # IS the thing M3_FIPS_STRICT requires (enforced in __init__ via the
            # _fips_validated flag set below).
            has_post = hasattr(self._libwolf, "wolfCrypt_GetStatus_fips")
            if has_post:
                status = self._libwolf.wolfCrypt_GetStatus_fips()
                if status != 0:
                    error_msg = WOLF_ERROR_CODES.get(status, f"Error code {status}")
                    logger.error(f"M3 Crypto: wolfSSL FIPS POST failed: {error_msg}")
                    raise RuntimeError(f"FIPS POST failed with status {status}")

            # The loaded library is the CMVP-VALIDATED FIPS module only when it
            # exposes the FIPS service symbols AND its POST passed. This flag
            # gates M3_FIPS_STRICT in __init__.
            self._fips_validated = has_post and has_entropy_cb
            if (_fips_mode() or _fips_strict()) and not self._fips_validated:
                logger.warning(
                    "M3 Crypto: loaded wolfSSL is the OPEN-SOURCE (non-FIPS) build "
                    "— hardened wolfCrypt crypto is active, but this is NOT the "
                    "CMVP-validated FIPS module. Set M3_FIPS_STRICT=1 only with the "
                    "commercial wolfSSL FIPS build."
                )

            # 4. Application-level power-up Known-Answer-Tests. Proves THIS
            #    process's bindings compute the documented answers before any
            #    real crypto is served. Runs on BOTH the open-source and FIPS
            #    builds (the vectors are correct for any conforming wolfCrypt).
            #    A KAT failure is fatal under any FIPS tier (re-raised below).
            self._run_self_tests()

            self._initialized = True
            _tier = "CMVP-validated FIPS" if self._fips_validated else "open-source (non-FIPS)"
            logger.info(f"M3 Crypto: wolfSSL backend initialized ({_tier} wolfCrypt).")

        except Exception as e:
            if (_fips_mode() or _fips_strict()):
                raise RuntimeError(
                    f"FATAL: FIPS 140-3 Compliance Mode Enforced, but FIPS-validated "
                    f"wolfSSL/wolfCrypt backend failed to initialize: {e}."
                )
            logger.warning(f"M3 Crypto: wolfSSL FIPS initialization failed ({e}). Falling back to DEFAULT.")
            self.backend = "DEFAULT"
            self._initialized = False

    def _wolf_aes_gcm_encrypt_fixed_nonce(self, data: bytes, key: bytes, iv: bytes) -> bytes:
        """AES-256-GCM encrypt with a CALLER-SUPPLIED nonce — used ONLY by the
        power-up KAT so the ciphertext+tag is a deterministic known answer. The
        production encrypt path generates a fresh random nonce; nonce reuse is
        unsafe and this helper must never be used for real data."""
        if not self._libwolf:
            raise RuntimeError("wolfSSL library not loaded")
        out_buf = ctypes.create_string_buffer(len(data))
        tag_buf = ctypes.create_string_buffer(16)
        aes = ctypes.create_string_buffer(512)
        if self._libwolf.wc_AesGcmSetKey(aes, key, len(key)) != 0:
            raise RuntimeError("wc_AesGcmSetKey failed in KAT")
        ret = self._libwolf.wc_AesGcmEncrypt(
            aes, out_buf, data, len(data), iv, len(iv), tag_buf, 16, None, 0
        )
        if ret != 0:
            raise RuntimeError(f"wc_AesGcmEncrypt failed in KAT: {ret}")
        return iv + out_buf.raw + tag_buf.raw

    def _run_self_tests(self) -> None:
        """FIPS 140-3 power-up Known-Answer-Tests against the wolfCrypt path.

        Proves SHA-256, PBKDF2-SHA256, and AES-256-GCM each compute the
        documented answer before any real crypto is served. Raises on any
        mismatch — the caller (init) treats that as a fatal FIPS error. This is
        the application-level complement to wolfCrypt's own POST
        (wolfCrypt_GetStatus_fips): POST attests the module; these KATs attest
        that THIS process's bindings invoke it correctly.
        """
        # SHA-256 KAT
        got = self._wolf_sha256(_KAT_SHA256_INPUT)
        if got != _KAT_SHA256_DIGEST:
            raise RuntimeError(f"SHA-256 KAT failed: got {got}")

        # PBKDF2-SHA256 KAT (only if the binding is present)
        if hasattr(self._libwolf, "wc_PBKDF2"):
            key = self._wolf_pbkdf2_sha256(
                _KAT_PBKDF2_PASSWORD, _KAT_PBKDF2_SALT,
                _KAT_PBKDF2_ITERATIONS, _KAT_PBKDF2_DKLEN,
            )
            if key.hex() != _KAT_PBKDF2_KEY_HEX:
                raise RuntimeError(f"PBKDF2-SHA256 KAT failed: got {key.hex()}")

        # AES-256-GCM KAT — known-answer (fixed nonce) + authenticated round-trip.
        token = self._wolf_aes_gcm_encrypt_fixed_nonce(
            _KAT_AESGCM_PLAINTEXT, _KAT_AESGCM_KEY, _KAT_AESGCM_NONCE
        )
        ct_tag = token[len(_KAT_AESGCM_NONCE):]  # strip the prepended IV
        if ct_tag.hex() != _KAT_AESGCM_CT_TAG_HEX:
            raise RuntimeError(f"AES-256-GCM encrypt KAT failed: got {ct_tag.hex()}")
        # Decrypt must recover the plaintext (exercises GCM tag verification).
        back = self._wolf_aes_gcm_decrypt(token, _KAT_AESGCM_KEY)
        if back != _KAT_AESGCM_PLAINTEXT:
            raise RuntimeError("AES-256-GCM decrypt KAT failed: plaintext mismatch")

        logger.info("M3 Crypto: FIPS power-up KATs passed (SHA-256, PBKDF2, AES-256-GCM).")

    def _wolf_sha256(self, data: bytes) -> str:
        if not self._libwolf:
            raise RuntimeError("wolfSSL library not loaded")
        hash_buf = ctypes.create_string_buffer(32)
        ret = self._libwolf.wc_Sha256Hash(data, len(data), hash_buf)
        if ret != 0:
            raise RuntimeError(f"wc_Sha256Hash failed with code: {ret}")
        return hash_buf.raw.hex()

    def _wolf_aes_gcm_encrypt(self, data: bytes, key: bytes) -> bytes:
        if not self._libwolf:
            raise RuntimeError("wolfSSL library not loaded")

        iv = os.urandom(12)
        out_buf = ctypes.create_string_buffer(len(data))
        tag_buf = ctypes.create_string_buffer(16)

        aes = ctypes.create_string_buffer(512)
        ret_key = self._libwolf.wc_AesGcmSetKey(aes, key, len(key))
        if ret_key != 0:
            raise RuntimeError(f"wc_AesGcmSetKey failed with code: {ret_key}")

        ret_enc = self._libwolf.wc_AesGcmEncrypt(
            aes, out_buf, data, len(data),
            iv, len(iv), tag_buf, 16,
            None, 0
        )
        if ret_enc != 0:
            raise RuntimeError(f"wc_AesGcmEncrypt failed with code: {ret_enc}")

        return iv + out_buf.raw + tag_buf.raw

    def _wolf_aes_gcm_decrypt(self, token: bytes, key: bytes) -> bytes:
        if not self._libwolf:
            raise RuntimeError("wolfSSL library not loaded")

        if len(token) < 28:
            raise ValueError("Token too short for AES-GCM")

        iv = token[:12]
        tag = token[-16:]
        ciphertext = token[12:-16]

        out_buf = ctypes.create_string_buffer(len(ciphertext))

        aes = ctypes.create_string_buffer(512)
        ret_key = self._libwolf.wc_AesGcmSetKey(aes, key, len(key))
        if ret_key != 0:
            raise RuntimeError(f"wc_AesGcmSetKey failed with code: {ret_key}")

        ret_dec = self._libwolf.wc_AesGcmDecrypt(
            aes, out_buf, ciphertext, len(ciphertext),
            iv, len(iv), tag, 16,
            None, 0
        )
        if ret_dec != 0:
            raise RuntimeError(f"wc_AesGcmDecrypt failed with code: {ret_dec}")

        return out_buf.raw

    def pbkdf2_sha256(self, password: bytes, salt: bytes, iterations: int, length: int = 32) -> bytes:
        """Derive a key via PBKDF2-HMAC-SHA256.

        Routes key derivation through the FIPS boundary: under M3_FIPS_MODE the
        wolfCrypt path is mandatory and any miss/failure is fatal (never a silent
        fallback to the stdlib KDF). The DEFAULT backend uses the `cryptography`
        library's PBKDF2HMAC, which is byte-identical output for the same
        (password, salt, iterations, length) — so existing derived keys/secrets
        decrypt unchanged when not in FIPS mode.
        """
        if (_fips_mode() or _fips_strict()):
            if not self._initialized or self.backend != "WOLFSSL":
                raise RuntimeError("FIPS boundary violation: wolfCrypt is missing or failed to initialize.")

        if self.backend == "WOLFSSL" and self._initialized and self._libwolf:
            try:
                return self._wolf_pbkdf2_sha256(password, salt, iterations, length)
            except Exception as e:
                if (_fips_mode() or _fips_strict()):
                    raise RuntimeError(f"FIPS PBKDF2 derivation failed: {e}. Raising to prevent unsafe fallback.")
                logger.debug(f"M3 Crypto: wolfSSL PBKDF2 failed ({e}), using default fallback.")

        # Default Fallback: PBKDF2-HMAC-SHA256 via cryptography
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=length, salt=salt, iterations=iterations)
        return kdf.derive(password)

    def _wolf_pbkdf2_sha256(self, password: bytes, salt: bytes, iterations: int, length: int) -> bytes:
        """wolfCrypt PBKDF2-HMAC-SHA256 via wc_PBKDF2.

        Signature: int wc_PBKDF2(byte* out, const byte* passwd, int pLen,
                                 const byte* salt, int sLen, int iterations,
                                 int kLen, int typeH). typeH for SHA-256 is the
                                 wolfCrypt enum WC_SHA256 (== 2).
        """
        if not self._libwolf or not hasattr(self._libwolf, "wc_PBKDF2"):
            raise RuntimeError("wc_PBKDF2 not available in loaded wolfSSL library")
        # wc_PBKDF2's `hashType` is the LEGACY hash-OID enum (typeH), NOT the
        # newer `enum wc_HashType` where SHA-256 == 2. In the typeH enum SHA-256
        # == 6 (verified against wolfSSL 5.9.2: hashType=2 -> BAD_FUNC_ARG -173;
        # hashType=6 -> correct RFC-7914 digest). The mock can't catch this
        # because it ignores typeH — only a real wolfSSL build does.
        WC_SHA256_TYPEH = 6
        out_buf = ctypes.create_string_buffer(length)
        ret = self._libwolf.wc_PBKDF2(
            out_buf, password, len(password),
            salt, len(salt), iterations, length, WC_SHA256_TYPEH,
        )
        if ret != 0:
            raise RuntimeError(f"wc_PBKDF2 failed with code: {ret}")
        return out_buf.raw[:length]

    def sha256(self, data: bytes) -> str:
        """Returns the SHA-256 hex digest of the data."""
        if (_fips_mode() or _fips_strict()):
            if not self._initialized or self.backend != "WOLFSSL":
                raise RuntimeError("FIPS boundary violation: wolfCrypt is missing or failed to initialize.")

        if self.backend == "WOLFSSL" and self._initialized and self._libwolf:
            try:
                return self._wolf_sha256(data)
            except Exception as e:
                if (_fips_mode() or _fips_strict()):
                    raise RuntimeError(f"FIPS SHA-256 Hashing Failed: {e}. Raising to prevent unsafe fallback.")
                logger.debug(f"M3 Crypto: wolfSSL SHA-256 hashing failed ({e}), using default fallback.")

        # Default Fallback
        return hashlib.sha256(data).hexdigest()

    def encrypt(self, data: bytes, key: bytes) -> bytes:
        """Encrypts data using AES-256-GCM."""
        if (_fips_mode() or _fips_strict()):
            if not self._initialized or self.backend != "WOLFSSL":
                raise RuntimeError("FIPS boundary violation: wolfCrypt is missing or failed to initialize.")

        if self.backend == "WOLFSSL" and self._initialized and self._libwolf:
            try:
                return self._wolf_aes_gcm_encrypt(data, key)
            except Exception as e:
                if (_fips_mode() or _fips_strict()):
                    raise RuntimeError(f"FIPS AES-GCM Encrypt Failed: {e}. Raising to prevent unsafe leakage.")
                logger.debug(f"M3 Crypto: wolfSSL AES-GCM encrypt failed ({e}), using default fallback.")

        # Default Fallback: AES-256-GCM via cryptography
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        aesgcm = AESGCM(key)
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(nonce, data, None)
        return nonce + ciphertext

    def decrypt(self, token: bytes, key: bytes) -> bytes:
        """Decrypts data using AES-256-GCM."""
        if (_fips_mode() or _fips_strict()):
            if not self._initialized or self.backend != "WOLFSSL":
                raise RuntimeError("FIPS boundary violation: wolfCrypt is missing or failed to initialize.")

        if self.backend == "WOLFSSL" and self._initialized and self._libwolf:
            try:
                return self._wolf_aes_gcm_decrypt(token, key)
            except Exception as e:
                if (_fips_mode() or _fips_strict()):
                    raise RuntimeError(f"FIPS AES-GCM Decrypt Failed: {e}. Raising to prevent unsafe leakage.")
                logger.debug(f"M3 Crypto: wolfSSL AES-GCM decrypt failed ({e}), using default fallback.")

        # Default Fallback: AES-256-GCM via cryptography
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        aesgcm = AESGCM(key)
        nonce = token[:12]
        ciphertext = token[12:]
        return aesgcm.decrypt(nonce, ciphertext, None)

    def lock_key(self):
        """Mandatory FIPS 140-3 Key Access Management: LOCK."""
        if self.backend == "WOLFSSL" and self._initialized:
            if hasattr(self._libwolf, "PRIVATE_KEY_LOCK"):
                self._libwolf.PRIVATE_KEY_LOCK()
            logger.debug("M3 Crypto: Private key LOCKED.")

    def unlock_key(self):
        """Mandatory FIPS 140-3 Key Access Management: UNLOCK."""
        if self.backend == "WOLFSSL" and self._initialized:
            if hasattr(self._libwolf, "PRIVATE_KEY_UNLOCK"):
                self._libwolf.PRIVATE_KEY_UNLOCK()
            logger.debug("M3 Crypto: Private key UNLOCKED.")

    def get_ssl_context(self):
        """Returns a FIPS-hardened SSLContext restricted to TLS 1.3."""
        if self.backend == "WOLFSSL" and self._initialized:
            try:
                import wolfssl
                # Hardened wolfSSL context
                ctx = wolfssl.SSLContext(wolfssl.PROTOCOL_TLS_CLIENT)
                ctx.set_ciphers("TLS13-AES256-GCM-SHA384:TLS13-AES128-GCM-SHA256")
                ctx.options |= wolfssl.OP_NO_TLSv1 | wolfssl.OP_NO_TLSv1_1 | wolfssl.OP_NO_TLSv1_2
                logger.info("M3 Crypto: Created FIPS-hardened TLS 1.3 context (wolfSSL).")
                return ctx
            except (ImportError, AttributeError) as e:
                logger.warning(f"M3 Crypto: wolfssl module unavailable ({e}), using FIPS-hardened standard fallback.")

        # Default Fallback (Standard Python ssl)
        import ssl
        ctx = ssl.create_default_context()
        # Enforce TLS 1.3 via minimum_version (modern API; OP_NO_TLS* deprecated in 3.10+)
        if hasattr(ssl, "TLSVersion"):
            ctx.minimum_version = ssl.TLSVersion.TLSv1_3
            ctx.maximum_version = ssl.TLSVersion.TLSv1_3
        else:
            # Fallback for older Python without TLSVersion enum
            if hasattr(ssl, "OP_NO_SSLv2"): ctx.options |= ssl.OP_NO_SSLv2  # noqa: E701
            if hasattr(ssl, "OP_NO_SSLv3"): ctx.options |= ssl.OP_NO_SSLv3  # noqa: E701
            if hasattr(ssl, "OP_NO_TLSv1"): ctx.options |= ssl.OP_NO_TLSv1  # noqa: E701
            if hasattr(ssl, "OP_NO_TLSv1_1"): ctx.options |= ssl.OP_NO_TLSv1_1  # noqa: E701
            if hasattr(ssl, "OP_NO_TLSv1_2"): ctx.options |= ssl.OP_NO_TLSv1_2  # noqa: E701

        # Configure FIPS-validated ciphers for default SSL context if possible
        try:
            ctx.set_ciphers("ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256")
        except ssl.SSLError:
            pass

        return ctx

# Global Instance
provider = CryptoProvider(BACKEND)

def get_sha256(data: bytes) -> str:
    return provider.sha256(data)


def active_crypto_status() -> dict:
    """Read-only summary of the live crypto backend for `m3 doctor`.

    Returns {backend, fips_mode, fips_strict, fips_validated, lib_path,
    integrity_pinned, summary}. Best-effort; never raises.
    """
    lib_path = getattr(provider, "_lib_path", None)
    # Compute the loaded library's SHA-256 so the operator can self-pin it via
    # M3_WOLFSSL_SHA256 (they build their own wolfSSL, so this IS the trusted
    # hash to pin). Best-effort — never let doctor fail on a read error.
    lib_sha256 = None
    if lib_path:
        try:
            _h = hashlib.sha256()
            with open(lib_path, "rb") as _f:
                for _chunk in iter(lambda: _f.read(1 << 20), b""):
                    _h.update(_chunk)
            lib_sha256 = _h.hexdigest()
        except OSError:
            lib_sha256 = None
    out = {
        "backend": getattr(provider, "backend", "DEFAULT"),
        "fips_mode": _fips_mode(),
        "fips_strict": _fips_strict(),
        "fips_validated": getattr(provider, "_fips_validated", False),
        "lib_path": lib_path,
        "lib_sha256": lib_sha256,
        "integrity_pinned": bool(os.environ.get("M3_WOLFSSL_SHA256", "").strip()),
        "summary": "",
    }
    if out["backend"] != "WOLFSSL":
        out["summary"] = (
            "DEFAULT (Python cryptography/hashlib). Approved algorithms only; "
            "no wolfCrypt loaded. Set M3_FIPS_MODE=1 to route through wolfCrypt."
        )
    elif out["fips_validated"]:
        out["summary"] = (
            f"wolfCrypt CMVP-validated FIPS module — loaded from {out['lib_path']}"
            + (" (integrity-pinned)" if out["integrity_pinned"] else "")
        )
    else:
        out["summary"] = (
            f"wolfCrypt OPEN-SOURCE (non-FIPS) build — loaded from {out['lib_path']}"
            + (" (integrity-pinned)" if out["integrity_pinned"] else "")
            + ". Hardened + fail-closed, but NOT the CMVP-validated module "
              "(M3_FIPS_STRICT would refuse this)."
        )
    return out
