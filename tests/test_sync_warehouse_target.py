"""Where the scheduled sync finds the warehouse.

A launchd/systemd/Task Scheduler job does not see the shell's environment, so a
warehouse configured only as M3_POSTGRES_SERVER in a shell rc was invisible to
it and the sync skipped silently. The DSN (env or stored secret) names the host
too. Addresses here are RFC 5737/6761 reserved; nothing is contacted.
"""
from __future__ import annotations

import logging

import m3_sdk
import sync_all


def test_the_host_env_var_wins(monkeypatch):
    monkeypatch.setenv("M3_POSTGRES_SERVER", "192.0.2.10")
    monkeypatch.setattr(m3_sdk, "resolve_warehouse_dsn",
                        lambda: "postgresql://u@db.example.invalid:6543/w")
    assert sync_all.warehouse_target() == ("192.0.2.10", 5432, "M3_POSTGRES_SERVER")


def test_without_the_host_env_the_dsn_supplies_host_and_port(monkeypatch):
    monkeypatch.setattr(m3_sdk, "resolve_warehouse_dsn",
                        lambda: "postgresql://u:p@db.example.invalid:6543/warehouse")
    assert sync_all.warehouse_target() == ("db.example.invalid", 6543, "warehouse DSN")


def test_a_dsn_without_a_host_is_reported_not_used(monkeypatch, caplog):
    monkeypatch.setattr(m3_sdk, "resolve_warehouse_dsn", lambda: "postgresql:///warehouse")
    with caplog.at_level(logging.WARNING):
        assert sync_all.warehouse_target() is None
    assert "no host" in caplog.text


def test_nothing_configured_is_none(monkeypatch):
    monkeypatch.setattr(m3_sdk, "resolve_warehouse_dsn", lambda: None)
    assert sync_all.warehouse_target() is None
