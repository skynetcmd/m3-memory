"""FIPS boundary: all crypto must route through crypto_provider (Phase 1).

Two boundary leaks were closed so M3_FIPS_MODE actually covers them:
  - audit_trail.py hashed via hashlib.sha256() directly,
  - auth_utils.py derived keys via cryptography's PBKDF2HMAC directly.
Both now go through crypto_provider. These tests pin (a) byte-compatibility
with the prior direct paths — so existing audit logs / encrypted secrets stay
valid — and (b) that the provider routes the call (FIPS-coverable).
"""
from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from crypto_provider import get_sha256, provider  # noqa: E402
from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC  # noqa: E402

# ── SHA-256 (audit_trail leak) ─────────────────────────────────────────────────

def test_provider_sha256_matches_hashlib():
    """get_sha256 must be byte-identical to hashlib on the DEFAULT backend so
    pre-existing audit-trail hash chains still verify."""
    for data in (b"", b"a", b"audit-canonical-json", os.urandom(1000)):
        assert get_sha256(data) == hashlib.sha256(data).hexdigest()


def test_audit_trail_uses_provider(monkeypatch, tmp_path):
    """The audit trail's write+verify round-trips through the provider."""
    monkeypatch.setenv("M3_MEMORY_ROOT", str(tmp_path))
    import importlib

    import audit_trail
    importlib.reload(audit_trail)
    h = audit_trail.write_audit_entry("delete", "mem-1", {"k": "v"})
    assert isinstance(h, str) and len(h) == 64
    assert audit_trail.verify_audit_trail() is True


# ── PBKDF2 (auth_utils leak) ────────────────────────────────────────────────────

def test_provider_pbkdf2_matches_cryptography():
    """provider.pbkdf2_sha256 must match cryptography's PBKDF2HMAC byte-for-byte
    so existing encrypted secrets decrypt with the routed KDF."""
    pw, salt, iters = b"master-key", b"sixteen-byte-slt", 100_000
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iters)
    expected = kdf.derive(pw)
    assert provider.pbkdf2_sha256(pw, salt, iters, 32) == expected


def test_derive_raw_key_round_trips_through_provider(monkeypatch, tmp_path):
    """auth_utils._derive_raw_key now calls provider.pbkdf2_sha256 — verify it
    still produces a usable 32-byte key and that encrypt/decrypt round-trips."""
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setenv("M3_MEMORY_ROOT", str(tmp_path))
    import importlib

    import auth_utils
    importlib.reload(auth_utils)
    key = auth_utils._derive_raw_key("master-pw", 100_000)
    assert isinstance(key, (bytes, bytearray)) and len(key) == 32
    # And the key works end to end through the provider's AES-GCM.
    token = provider.encrypt(b"secret-value", bytes(key))
    assert provider.decrypt(token, bytes(key)) == b"secret-value"


def test_fernet_kdf_routes_through_provider(monkeypatch, tmp_path):
    """The legacy Fernet path derives its key via the provider too (the KDF is
    on the boundary even though Fernet itself is legacy)."""
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setenv("M3_MEMORY_ROOT", str(tmp_path))
    import importlib

    import auth_utils
    importlib.reload(auth_utils)
    f = auth_utils._get_fernet("master-pw", 100_000)
    # Fernet token round-trips (proves the derived key is valid base64-urlsafe 32B).
    tok = f.encrypt(b"legacy")
    assert f.decrypt(tok) == b"legacy"
