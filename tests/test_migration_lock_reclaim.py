"""Migration-lock staleness reclaim (footgun fix, 2026-06-27).

A process killed while holding the Python-fallback migration lock leaves the
lock file behind, which previously wedged EVERY subsequent migration for the
full 120s timeout and then hard-errored. `_reclaim_stale_lock` removes such a
lock IFF its owner is provably gone — and must NEVER steal a lock held by a
live process. These tests pin both the reclaim and the safety guarantee.
"""
from __future__ import annotations

import os
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import m3_sdk  # noqa: E402


def _write_lock(path, content: str, *, mtime: float | None = None):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def test_reclaims_lock_from_dead_same_host_pid(tmp_path):
    """Same host, owner PID not alive -> reclaim."""
    lock = tmp_path / ".migration.lock"
    dead_pid = 2_000_000_000  # astronomically unlikely to be a live PID
    _write_lock(lock, f"{dead_pid} {socket.gethostname()} {int(time.time())}")
    assert m3_sdk._reclaim_stale_lock(str(lock)) is True
    assert not lock.exists()


def test_never_reclaims_live_same_host_pid(tmp_path):
    """Same host, owner PID IS alive (this very process) -> must NOT reclaim."""
    lock = tmp_path / ".migration.lock"
    _write_lock(lock, f"{os.getpid()} {socket.gethostname()} {int(time.time())}")
    assert m3_sdk._reclaim_stale_lock(str(lock)) is False
    assert lock.exists(), "reclaimed a lock held by a LIVE process — data-race risk"


def test_cross_host_recent_lock_is_not_reclaimed(tmp_path):
    """Different host (can't probe PID) + fresh file -> leave it alone."""
    lock = tmp_path / ".migration.lock"
    _write_lock(lock, f"12345 some-other-host {int(time.time())}",
                mtime=time.time())  # brand new
    assert m3_sdk._reclaim_stale_lock(str(lock)) is False
    assert lock.exists()


def test_cross_host_ancient_lock_is_reclaimed(tmp_path):
    """Different host + file older than the max-age ceiling -> stale, reclaim."""
    lock = tmp_path / ".migration.lock"
    old = time.time() - (m3_sdk._MIGRATION_LOCK_MAX_AGE_S + 60)
    _write_lock(lock, "12345 some-other-host 0", mtime=old)
    assert m3_sdk._reclaim_stale_lock(str(lock)) is True
    assert not lock.exists()


def test_empty_stamp_recent_not_reclaimed(tmp_path):
    """Unparseable/empty stamp but recent file -> don't touch (fail safe)."""
    lock = tmp_path / ".migration.lock"
    _write_lock(lock, "", mtime=time.time())
    assert m3_sdk._reclaim_stale_lock(str(lock)) is False
    assert lock.exists()


def test_empty_stamp_ancient_is_reclaimed(tmp_path):
    """Empty stamp + ancient file -> stale, reclaim."""
    lock = tmp_path / ".migration.lock"
    old = time.time() - (m3_sdk._MIGRATION_LOCK_MAX_AGE_S + 60)
    _write_lock(lock, "", mtime=old)
    assert m3_sdk._reclaim_stale_lock(str(lock)) is True
    assert not lock.exists()


def test_missing_lock_is_reclaimable(tmp_path):
    """A vanished lock file is trivially 'reclaimed' (retry will re-create)."""
    lock = tmp_path / ".migration.lock"
    assert not lock.exists()
    assert m3_sdk._reclaim_stale_lock(str(lock)) is True


def test_pid_alive_self_is_true():
    assert m3_sdk._pid_alive(os.getpid()) is True


def test_pid_alive_dead_is_false():
    assert m3_sdk._pid_alive(2_000_000_000) is False


def test_pid_alive_zero_is_false():
    assert m3_sdk._pid_alive(0) is False


def test_lock_owner_stamp_is_parseable():
    """The stamp written on acquire must round-trip through the reclaim parser:
    'pid host epoch'."""
    stamp = m3_sdk._lock_owner_stamp()
    parts = stamp.split()
    assert len(parts) >= 3
    assert int(parts[0]) == os.getpid()
    assert parts[1] == socket.gethostname()
    assert int(parts[2]) > 0
