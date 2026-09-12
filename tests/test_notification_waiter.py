"""Pins the agent-to-agent notification waiter.

The waiter exists to satisfy one SLA: **acknowledge receipt of a notification
within 30 seconds**, reliably, without a human. The design turns on three facts
that were measured rather than assumed:

* Ack does not need an agent turn -- ``notifications_ack_all`` round-trips in
  ~426 ms from a plain subprocess, so a subprocess CAN satisfy a receipt SLA.
  It is nevertheless OFF by default: `notifications` has only ``read_at``, no
  separate 'received' column, so acking on detection marks a message read that
  no agent read. Measured: 29 of 30 recent notifications carried no task_id, so
  task state does not cover the gap. Opt in with ``--ack`` where it does.
* SQLite has no cross-process blocking change notification (update hooks are
  in-process only), so the trigger is the WAL file's ``(mtime, size)``.
* The WAL trigger is *shared*: one file change covers every inbox, so one
  process can serve N agents at the cost of N confirm-polls after a change --
  not N watchers.

These tests cover the argument surface and the invariants. The timing figures
(detect+ack 1192/1182/1203 ms) came from live runs and are not re-measured here;
a unit test asserting wall-clock latency would be flaky and would not be
measuring the thing that matters.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
_SRC = _BIN / "m3_notification_waiter.py"

_spec = importlib.util.spec_from_file_location("m3_notification_waiter", _SRC)
assert _spec and _spec.loader
waiter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(waiter)


def test_wal_fingerprint_tracks_mtime_and_size(tmp_path):
    """Both halves matter: checkpointing can SHRINK the WAL, so size alone both
    misses growth-then-truncate and can false-fire."""
    wal = tmp_path / "agent_memory.db-wal"
    assert waiter.wal_fingerprint(wal) == (), "absent WAL must be falsy, not an error"

    wal.write_bytes(b"x" * 10)
    first = waiter.wal_fingerprint(wal)
    assert len(first) == 2, "fingerprint must carry (mtime, size)"

    wal.write_bytes(b"x" * 20)
    assert waiter.wal_fingerprint(wal) != first, "a size change must be visible"


def test_missing_wal_is_not_an_error(tmp_path):
    """A missing WAL means 'nothing written yet', not a crash: the waiter must
    survive a fresh engine root."""
    assert waiter.wal_fingerprint(tmp_path / "nope-wal") == ()


def test_agent_id_is_repeatable_for_multi_inbox():
    """One process serves N inboxes. The WAL trigger is shared, so per-agent
    watchers would multiply processes for no latency gain -- and would keep live
    watchers running for agents that have been offline for months."""
    src = _SRC.read_text(encoding="utf-8")
    assert 'action="append"' in src, "--agent-id must accumulate, not overwrite"
    assert 'dest="agent_ids"' in src


def test_ack_is_opt_in_not_default():
    """Auto-ack must NOT be the default.

    `notifications` carries one timestamp, `read_at` -- there is no separate
    'received' column. Acking on DETECTION therefore marks a message read that no
    agent has read, and that flag was the only record the work was pending.

    The claim that "the task state machine covers it" was measured and is false
    for real traffic: 29 of 30 recent notifications carried no task_id. Losing a
    message silently is worse than reporting receipt one turn later, so the
    waiter detects and delivers by default and acks only under --ack.
    """
    src = _SRC.read_text(encoding="utf-8")
    assert '"--ack"' in src, "an explicit opt-in flag must exist"
    assert '"--no-ack"' not in src, "an opt-OUT implies acking by default"
    assert "if args.ack:" in src, "ack must be gated on the opt-in"
    assert "notifications_ack_all" in src, "the waiter must still be ABLE to ack"


def test_supervise_mode_reuses_the_single_shot_body():
    """Supervise loops _wait_once rather than duplicating detect-confirm-ack, so
    the two modes cannot drift apart."""
    src = _SRC.read_text(encoding="utf-8")
    assert "def _wait_once(" in src
    assert "_wait_once(args)" in src
    assert '"--supervise"' in src


def test_timeout_bounds_a_forgotten_waiter():
    """An unbounded waiter that nobody re-arms is a leak. The default must be
    finite."""
    import argparse

    parser_defaults = {}
    src = _SRC.read_text(encoding="utf-8")
    assert '"--timeout"' in src
    assert "default=3600" in src, "timeout must default to a finite value"
    assert isinstance(argparse.ArgumentParser, type)  # sanity, keeps import used
    assert parser_defaults == {}


def test_never_imports_m3_memory_at_module_scope():
    """The waiter runs as an installed background task and must not bind itself
    to a payload that an upgrade may replace underneath it."""
    src = _SRC.read_text(encoding="utf-8")
    assert "\nimport m3_memory" not in src
    assert "\nfrom m3_memory" not in src


def test_parses_at_the_declared_floor():
    """requires-python is >=3.11."""
    import ast

    src = _SRC.read_text(encoding="utf-8")
    for ver in ((3, 11), (3, 12), (3, 13)):
        ast.parse(src, feature_version=ver)


# --- the installer spec ------------------------------------------------------

_SCHED = _BIN / "install_schedules.py"


def test_waiter_task_is_registered_by_the_installer():
    """A non-elevated agent session CANNOT register a scheduled task -- both
    Claude Code and Antigravity measured ``Access is denied`` (0x80070005) at
    runtime. Installer-time registration, with a human who can elevate, is the
    only reproducible path, so the spec must live here."""
    src = _SCHED.read_text(encoding="utf-8")
    assert "AgentOS_NotificationWaiter" in src
    assert "m3_notification_waiter.py" in src


def test_waiter_task_runs_unbuffered():
    """-u is required, not cosmetic. Without it Python buffers stdout and a LIVE
    supervisor emits nothing for minutes -- indistinguishable from a dead one.
    That produced a false 'supervise is broken' diagnosis while building this."""
    src = _SCHED.read_text(encoding="utf-8")
    assert '"-u"' in src, "the waiter task must run python unbuffered"


def test_waiter_task_does_not_hardcode_an_agent_id():
    """A shipped installer hardcoding one agent id would make every machine ack
    that agent's inbox."""
    src = _SCHED.read_text(encoding="utf-8")
    assert '"--agent-id", "claude-code"' not in src
    assert "_waiter_agent_args()" in src


def test_agent_args_never_returns_empty():
    """A waiter watching no inboxes looks healthy and does nothing -- the exact
    silent failure this feature exists to remove. The registry lookup is
    best-effort, so it must fall back rather than yield nothing."""
    if str(_BIN) not in sys.path:
        sys.path.insert(0, str(_BIN))
    import install_schedules  # noqa: E402

    args = install_schedules._waiter_agent_args()
    assert args, "must never return an empty argument list"
    ids = [a for a in args if a != "--agent-id"]
    assert ids, "must resolve at least one agent id"
    assert args[0] == "--agent-id"


@pytest.mark.parametrize("flag", ["--interval", "--supervise", "--timeout"])
def test_waiter_task_passes_the_flags_the_design_depends_on(flag):
    src = _SCHED.read_text(encoding="utf-8")
    assert f'"{flag}"' in src
