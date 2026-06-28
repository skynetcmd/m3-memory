"""Tests for the install/upgrade backup reminder + CDW detection.

The reminder must:
  - always advise backing up the engine DBs;
  - when a CDW (PostgreSQL warehouse) is configured, name WHERE data auto-syncs
    and stress that sync is not a backup;
  - NEVER leak the PG_URL password (host only).
"""
from __future__ import annotations

import contextlib
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from m3_memory import installer  # noqa: E402


def _reminder_output():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        installer._print_backup_reminder()
    return buf.getvalue()


def test_reminder_always_advises_backup(monkeypatch):
    for k in ("PG_URL", "POSTGRES_SERVER", "SYNC_TARGET_IP"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(installer, "_detect_cdw_target", lambda: None)
    out = _reminder_output()
    assert "back up" in out.lower()
    assert "M3_ENGINE_ROOT" in out
    # No-CDW branch points the user at how to set one up.
    assert "data warehouse" in out.lower()


def test_cdw_via_env_ip_is_named(monkeypatch):
    monkeypatch.delenv("PG_URL", raising=False)
    monkeypatch.setenv("SYNC_TARGET_IP", "192.0.2.50")
    out = _reminder_output()
    assert "192.0.2.50" in out
    assert "not a" in out.lower() and "backup" in out.lower()


def test_pg_url_host_shown_password_never_leaked(monkeypatch):
    for k in ("POSTGRES_SERVER", "SYNC_TARGET_IP"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_URL", "postgresql://app:TOPSECRET@warehouse.example:5432/m3")
    out = _reminder_output()
    assert "warehouse.example" in out          # host shown
    assert "TOPSECRET" not in out              # password NEVER shown
    assert "app:" not in out                   # nor the userinfo


def test_detect_returns_none_with_no_config(monkeypatch):
    for k in ("PG_URL", "POSTGRES_SERVER", "SYNC_TARGET_IP"):
        monkeypatch.delenv(k, raising=False)
    # Make the vault lookup a no-op so the test doesn't depend on a real vault.
    import m3_sdk
    monkeypatch.setattr(m3_sdk.M3Context, "get_secret", lambda self, k: None, raising=False)
    assert installer._detect_cdw_target() is None


def test_detect_extracts_host_from_pg_url(monkeypatch):
    for k in ("POSTGRES_SERVER", "SYNC_TARGET_IP"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PG_URL", "postgresql://u:p@db.internal:5432/x")
    assert installer._detect_cdw_target() == "db.internal"
