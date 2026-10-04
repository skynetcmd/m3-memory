"""The cognitive loop yields to HALT_m3 while sleeping, not only between cycles.

Hazard: the loop slept a whole --interval (300s on Unix) waiting only for the
stop event, while still registered as a DB writer, so setup's quiesce waited
its full timeout and then force-killed it.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import m3_cognitive_loop as loop  # noqa: E402


def test_sleep_returns_when_halt_is_raised(monkeypatch):
    monkeypatch.setattr(loop, "_STOP_EVENT", asyncio.Event())
    raised_at = time.monotonic() + 0.3
    monkeypatch.setattr(loop.m3_halt, "halt_is_active",
                        lambda role=None: time.monotonic() >= raised_at)
    t0 = time.monotonic()
    asyncio.run(loop._sleep_or_halt(60.0, poll_s=0.1))
    assert time.monotonic() - t0 < 5.0


def test_sleep_runs_its_course_without_halt(monkeypatch):
    monkeypatch.setattr(loop, "_STOP_EVENT", asyncio.Event())
    monkeypatch.setattr(loop.m3_halt, "halt_is_active", lambda role=None: False)
    t0 = time.monotonic()
    asyncio.run(loop._sleep_or_halt(0.5, poll_s=0.1))
    assert time.monotonic() - t0 >= 0.45
