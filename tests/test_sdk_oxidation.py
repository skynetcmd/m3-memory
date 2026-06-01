"""Unit tests for M3 SDK native Rust telemetry, locking, and circuit breaker integration.

Verifies:
1. When m3_core_rs implements the oxidized functions/classes, m3_sdk calls them successfully.
2. When m3_core_rs is absent, disabled, or missing these attributes, the system falls back open gracefully to the Python implementation.
"""
from __future__ import annotations

import os
import sys
import time
import pytest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import m3_sdk


class MockTelemetry:
    def __init__(self, cpu, ram, gpu, thermal):
        self.cpu_total = cpu
        self.ram_total = ram
        self.gpu_total = gpu
        self.thermal = thermal


class MockNativeMigrationLock:
    def __init__(self, path):
        self.path = path
        self.acquired = False
        self.released = False

    def acquire(self, timeout):
        self.acquired = True
        return True

    def release(self):
        self.released = True


class MockNativeCircuitBreaker:
    def __init__(self, threshold, cooldown):
        self.threshold = threshold
        self.cooldown = cooldown
        self.checked = False
        self.success_recorded = False
        self.failure_recorded = False

    def check(self):
        self.checked = True
        return True

    def record_success(self):
        self.success_recorded = True

    def record_failure(self):
        self.failure_recorded = True


def test_get_system_telemetry_fallback(tmp_path, monkeypatch):
    """Fallback works cleanly when m3_core_rs is disabled or missing get_native_telemetry."""
    monkeypatch.setattr(m3_sdk, "M3_CORE_RS_DISABLE", True)
    
    db_file = str(tmp_path / "test.db")
    ctx = m3_sdk.M3Context(db_path=db_file)
    telemetry = ctx.get_system_telemetry()
    assert "cpu_total" in telemetry
    assert "ram_total" in telemetry


def test_get_system_telemetry_native(tmp_path, monkeypatch):
    """FFI fast path for telemetry is queried successfully when present in m3_core_rs."""
    monkeypatch.setattr(m3_sdk, "M3_CORE_RS_DISABLE", False)
    
    # Mock m3_core_rs module in sys.modules
    mock_rs = mock.Mock()
    mock_rs.get_native_telemetry.return_value = MockTelemetry(12.5, 45.0, 0.0, "Nominal")
    
    monkeypatch.setitem(sys.modules, "m3_core_rs", mock_rs)
    
    db_file = str(tmp_path / "test.db")
    ctx = m3_sdk.M3Context(db_path=db_file)
    telemetry = ctx.get_system_telemetry()
    
    assert telemetry["cpu_total"] == 12.5
    assert telemetry["ram_total"] == 45.0
    assert telemetry["thermal"] == "Nominal"
    mock_rs.get_native_telemetry.assert_called_once()


def test_migration_lock_fallback(tmp_path, monkeypatch):
    """Fallback works cleanly when m3_core_rs is disabled or missing NativeMigrationLock."""
    monkeypatch.setattr(m3_sdk, "M3_CORE_RS_DISABLE", True)
    
    lock_root = tmp_path / "config"
    lock_root.mkdir()
    monkeypatch.setattr(m3_sdk, "get_m3_config_root", lambda: str(lock_root))
    
    with m3_sdk.migration_lock():
        assert os.path.exists(lock_root / ".migration.lock")
        
    assert not os.path.exists(lock_root / ".migration.lock")


def test_migration_lock_native(tmp_path, monkeypatch):
    """FFI fast path for migration lock is acquired and released when present in m3_core_rs."""
    monkeypatch.setattr(m3_sdk, "M3_CORE_RS_DISABLE", False)
    
    lock_root = tmp_path / "config"
    lock_root.mkdir()
    monkeypatch.setattr(m3_sdk, "get_m3_config_root", lambda: str(lock_root))
    
    # Mock m3_core_rs in sys.modules
    mock_rs = mock.Mock()
    # Mock call constructor
    mock_lock = None
    def ctor(path):
        nonlocal mock_lock
        mock_lock = MockNativeMigrationLock(path)
        return mock_lock
    mock_rs.NativeMigrationLock = ctor
    
    monkeypatch.setitem(sys.modules, "m3_core_rs", mock_rs)
    
    with m3_sdk.migration_lock():
        pass
        
    assert mock_lock is not None
    assert mock_lock.acquired is True
    assert mock_lock.released is True


def test_circuit_breaker_fallback(tmp_path, monkeypatch):
    """Fallback circuit breaker dict works cleanly when m3_core_rs is disabled."""
    monkeypatch.setattr(m3_sdk, "M3_CORE_RS_DISABLE", True)
    monkeypatch.setitem(m3_sdk._CIRCUITS, "test_svc", None)
    
    db_file = str(tmp_path / "test.db")
    ctx = m3_sdk.M3Context(db_path=db_file)
    assert ctx._check_circuit("test_svc") is True
    
    ctx._record_failure("test_svc", custom_cooldown=10.0)
    ctx._record_failure("test_svc", custom_cooldown=10.0)
    ctx._record_failure("test_svc", custom_cooldown=10.0)
    
    assert ctx._check_circuit("test_svc") is False
    
    ctx._record_success("test_svc")
    assert ctx._check_circuit("test_svc") is True


def test_circuit_breaker_native(tmp_path, monkeypatch):
    """FFI fast path for Circuit Breaker works when present in m3_core_rs."""
    monkeypatch.setattr(m3_sdk, "M3_CORE_RS_DISABLE", False)
    
    # Clean state
    monkeypatch.setitem(m3_sdk._CIRCUITS, "native_svc", None)
    
    # Mock m3_core_rs
    mock_rs = mock.Mock()
    mock_breaker = None
    def ctor(threshold, cooldown):
        nonlocal mock_breaker
        mock_breaker = MockNativeCircuitBreaker(threshold, cooldown)
        return mock_breaker
    mock_rs.NativeCircuitBreaker = ctor
    
    monkeypatch.setitem(sys.modules, "m3_core_rs", mock_rs)
    
    db_file = str(tmp_path / "test.db")
    ctx = m3_sdk.M3Context(db_path=db_file)
    
    # Check circuit initializes and registers
    assert ctx._check_circuit("native_svc") is True
    assert mock_breaker is not None
    assert mock_breaker.checked is True
    
    # Record success
    ctx._record_success("native_svc")
    assert mock_breaker.success_recorded is True
    
    # Record failure
    ctx._record_failure("native_svc")
    assert mock_breaker.failure_recorded is True
