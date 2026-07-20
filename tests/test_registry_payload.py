"""Tests for the shared coordination-file schema (bin/m3_core/registry_payload.py).

The schema is the single source of truth for PID-registry + single-instance-lock
files; these pin build/parse round-trips, reserved-key shadowing, and malformed
tolerance so the writers (m3_halt) and readers (m3_halt + a future dashboard
panel) can never drift.
"""
from __future__ import annotations

import sys
from pathlib import Path

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from m3_core.registry_payload import (  # noqa: E402
    PROTOCOL_VERSION,
    RESERVED_PAYLOAD_KEYS,
    ProcInfo,
    build_payload,
    parse_payload,
)


def test_build_payload_shape():
    p = build_payload("dashboard", "/x/engine", pid=123, create_time=99.5,
                      extra={"host": "127.0.0.1", "port": 8088})
    assert p["pid"] == 123
    assert p["role"] == "dashboard"
    assert p["create_time"] == 99.5
    assert p["engine_root"] == "/x/engine"
    assert p["protocol"] == PROTOCOL_VERSION
    assert p["extra"] == {"host": "127.0.0.1", "port": 8088}
    assert isinstance(p["started_at"], str) and "T" in p["started_at"]


def test_build_payload_no_extra_omits_key():
    p = build_payload("r", "/e", pid=1, create_time=None)
    assert "extra" not in p
    assert p["create_time"] is None


def test_extra_cannot_shadow_reserved_keys():
    # A caller passing a reserved key inside extra must not be able to override
    # the real field — defense in depth.
    p = build_payload("r", "/e", pid=7, create_time=1.0,
                      extra={"pid": 999, "role": "evil", "host": "h"})
    assert p["pid"] == 7 and p["role"] == "r"  # reserved fields intact
    assert "pid" not in p["extra"] and "role" not in p["extra"]
    assert p["extra"] == {"host": "h"}  # only the non-reserved survives


def test_reserved_keys_frozenset():
    for k in ("pid", "role", "started_at", "create_time", "engine_root",
              "protocol", "extra"):
        assert k in RESERVED_PAYLOAD_KEYS


def test_build_parse_round_trip():
    p = build_payload("embed-server", "/e", pid=42, create_time=3.5,
                      extra={"port": 8082})
    info = parse_payload(p, Path("embed-server.lock"))
    assert isinstance(info, ProcInfo)
    assert info.pid == 42 and info.role == "embed-server"
    assert info.create_time == 3.5
    assert info.extra == {"port": 8082}
    assert info.path == Path("embed-server.lock")


def test_parse_none_on_non_dict():
    assert parse_payload("not a dict", Path("x.lock")) is None
    assert parse_payload(None, Path("x.lock")) is None
    assert parse_payload([1, 2, 3], Path("x.lock")) is None


def test_parse_none_on_missing_or_bad_pid():
    assert parse_payload({}, Path("x.lock")) is None
    assert parse_payload({"pid": "abc"}, Path("x.lock")) is None
    assert parse_payload({"pid": 0}, Path("x.lock")) is None
    assert parse_payload({"pid": -5}, Path("x.lock")) is None


def test_parse_role_falls_back_to_stem():
    # A legacy entry without a role uses the file stem so it still identifies.
    info = parse_payload({"pid": 5}, Path("cognitive-loop.lock"))
    assert info is not None and info.role == "cognitive-loop.lock".rsplit(".", 1)[0] \
        or info.role == "cognitive-loop"  # Path.stem drops one suffix


def test_parse_tolerates_missing_create_time_and_extra():
    info = parse_payload({"pid": 5, "role": "r"}, Path("r.lock"))
    assert info is not None
    assert info.create_time is None  # older entry → None, not a crash
    assert info.extra == {}  # never None (§3)


def test_parse_bad_create_time_becomes_none():
    info = parse_payload({"pid": 5, "create_time": "not-a-float"}, Path("r.lock"))
    assert info is not None and info.create_time is None
