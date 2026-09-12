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


def test_help_renders_without_raising():
    """``--help`` must actually print help.

    argparse treats help strings as %-format templates, so a LITERAL ``%`` in a
    help string raises ``ValueError: badly formed help string`` the moment the
    formatter runs. Nothing catches it at import or at parse time -- only when a
    user asks for help, which is exactly when they are least able to diagnose it.

    This shipped: the --ack help text quoted "false for ~97% of real traffic" and
    ``m3_notification_waiter.py --help`` crashed instead of printing. The rest of
    the suite passed throughout, because no test had ever invoked --help.

    Asserting on the SUBPROCESS rather than calling format_help() in-process
    keeps this honest about the exit code a user would actually see.
    """
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(_SRC), "--help"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"--help exited {proc.returncode}; stderr={proc.stderr}"
    assert "--ack" in proc.stdout, proc.stdout


def test_no_literal_percent_in_argparse_help():
    """The general form of the bug above, so a future edit cannot reintroduce it
    in a different flag's help text and go unnoticed until someone runs --help.

    A literal ``%`` is only legal in argparse help as ``%%``; the one meaningful
    single-% form is the ``%(default)s`` style interpolation.

    Parsed with ``ast`` rather than matched with a regex. A regex over ``help=``
    was tried first and silently MISSED the real defect, because the offending
    string was an implicitly-concatenated literal spanning five lines inside
    parentheses -- so the test passed against the very code that crashed. A test
    that goes green on the bug it was written for is worse than no test.
    """
    import ast
    import re

    interp = re.compile(r"%\((?:default|prog|type|choices|const|dest|metavar)\)[sdr]")
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    checked = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "help":
                continue
            # ast folds implicit concatenation, so a 5-line parenthesised
            # literal arrives here as one complete Constant.
            if not isinstance(kw.value, ast.Constant) or not isinstance(kw.value.value, str):
                continue
            checked += 1
            stripped = interp.sub("", kw.value.value).replace("%%", "")
            assert "%" not in stripped, (
                "literal '%' in argparse help -- argparse raises 'badly formed "
                "help string' when --help is rendered. Write it as 'percent' or "
                "escape it as '%%':\n" + kw.value.value
            )

    assert checked, "no argparse help= strings found; the scan matched nothing"


def test_subprocess_failures_are_reported_not_swallowed():
    """The defect that caused the 2026-09-12 outage.

    ``unread_ids`` returned a bare ``set()`` on any non-zero exit. An empty set
    is INDISTINGUISHABLE from an empty inbox, so the waiter ran for an hour
    detecting nothing, through three probes from another agent, while Task
    Scheduler reported Running and every health check reported OK.

    The underlying cause was trivial once visible -- the task launched an
    interpreter that could not ``import m3_memory`` unless the working directory
    happened to be the source checkout, and the task sets no WorkingDirectory.
    One line of stderr would have found it in seconds.
    """
    src = _SRC.read_text(encoding="utf-8")
    assert "def _warn(" in src, "no single owner for failure reporting"
    # Every failure path must route through _warn, not a bare return.
    assert src.count("_warn(") >= 4, (
        "each subprocess failure branch (poll: exception + rc, admin: exception "
        "+ rc) must report; found too few _warn call sites"
    )
    assert "detection is BLIND" in src, (
        "the poll failure must say what it COSTS, not just that a command failed"
    )


def test_failure_message_names_the_interpreter():
    """The error text alone ("No module named 'm3_memory'") does not say WHICH
    python said it, and the bug WAS the interpreter. Naming it is what turns a
    confusing message into a diagnosis."""
    src = _SRC.read_text(encoding="utf-8")
    assert "def _warn(" in src, "no failure-reporting helper to inspect"
    warn_calls = src[src.index("def _warn("):]
    assert "sys.executable!r" in warn_calls, (
        "failure reports must name the interpreter that failed"
    )


def test_no_bare_m3_and_no_hardcoded_interpreter_paths():
    """Three ways to launch the CLI, two of them broken.

    * bare ``m3`` resolves on PATH but, without M3_ENGINE_ROOT in the
      environment, targets a DIFFERENT store and begins migrating it from v000
      while returning rc 0 -- so an rc-only check records success.
    * a hardcoded absolute path bakes one machine's home directory into a
      shipped file: wrong on every other machine, and a real home path in a
      public repo.

    Only ``sys.executable`` is correct.
    """
    src = _SRC.read_text(encoding="utf-8")
    assert '"m3", "admin"' not in src, "bare `m3` on PATH ignores M3_ENGINE_ROOT"
    assert "pipx" not in src, "hardcoded interpreter path"
    assert "Users" not in src.replace("Users of", ""), "a real home path leaked in"
    # Count the SUBPROCESS invocations, do not require a minimum of them.
    #
    # This asserted >= 2 and went red when the Option D work moved unread_ids
    # and mark_received IN-PROCESS, leaving one legitimate subprocess. The test
    # was pinning the old DESIGN (how many subprocesses exist) rather than the
    # INVARIANT (that any subprocess uses the right interpreter) -- so a correct
    # refactor failed it. DESIGN_PHILOSOPHIES 12c: when a test fails after a
    # fix, ask whether the TEST encodes the bug before changing the code back.
    #
    # The invariant restated: EVERY `-m m3_memory.cli` launch goes through
    # sys.executable. Zero such launches is fine; a launch by any other route
    # is not.
    import re

    launches = re.findall(r'\[([^\]]*?)"-m",\s*"m3_memory\.cli"', src, re.S)
    for head in launches:
        assert "sys.executable" in head, (
            f"a CLI launch does not use sys.executable: {head.strip()[:80]!r}"
        )


def test_window_hiding_has_one_owner_and_uses_sw_hide():
    """SW_HIDE, never CREATE_NO_WINDOW.

    Measured 2026-09-12 in a real console: CREATE_NO_WINDOW suppresses the
    child's INHERITED stdout as well as the window, so a failing subprocess
    reports nothing -- which would defeat the fail-loud work above. SW_HIDE
    hides the window and keeps the pipes.
    """
    src = _SRC.read_text(encoding="utf-8")
    assert "def _no_window(" in src, "no single owner for the hide logic"
    assert "SW_HIDE" in src
    # Parsed, not grepped. The source DISCUSSES CREATE_NO_WINDOW in a comment
    # explaining why it is not used, and a substring check cannot tell a mention
    # from a use -- the same false positive that made an earlier regex-based
    # test pass against the very bug it was written for.
    import ast

    tree = ast.parse(src)
    used = {
        n.attr for n in ast.walk(tree)
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
        and n.value.id == "subprocess"
    }
    assert "CREATE_NO_WINDOW" not in used, (
        "CREATE_NO_WINDOW would swallow the stderr the fail-loud path depends on"
    )
    assert "STARTUPINFO" in used, "SW_HIDE must be applied via STARTUPINFO"
    starts = sum(
        1 for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "STARTUPINFO"
    )
    assert starts == 1, (
        f"the hide logic must not be duplicated per call site -- copies drift "
        f"(found {starts} STARTUPINFO() constructions)"
    )


def test_startup_sweep_runs_before_the_baseline():
    """Ordering is the whole point.

    The baseline is "what is unread right now", and the loop only reports ids
    appearing AFTER it. Anything already waiting -- arrived during a restart, a
    reboot, a --timeout expiry, or the hour this waiter spent blind -- is
    baselined away as pre-existing and never stamped. Sweeping AFTER the
    baseline would be useless; sweeping before it makes restart windows
    self-healing.
    """
    src = _SRC.read_text(encoding="utf-8")
    marker = '_m3_admin("notifications_mark_received", _a)'
    assert marker in src, "no startup sweep call in the waiter"
    sweep = src.index(marker)
    baseline = src.index("baselines = {a: unread_ids(a)")
    assert sweep < baseline, (
        "the startup sweep must run BEFORE the baseline is taken, or the "
        "stranded notifications it exists to catch are already discarded"
    )


def test_startup_sweep_records_receipt_never_consumes_read():
    """A sweep that acked would be worse than no sweep: it would mark messages
    read that no agent has read, destroying the only record that the work is
    pending. notifications_mark_received targets received_at IS NULL only."""
    src = _SRC.read_text(encoding="utf-8")
    # Assert presence before slicing: str.index raises ValueError, which fails
    # the test with a traceback instead of the reason.
    assert "STARTUP SWEEP" in src, "no startup sweep in the waiter"
    sweep_region = src[src.index("STARTUP SWEEP"):src.index("baselines = {a:")]
    assert "notifications_mark_received" in sweep_region
    assert "ack" not in sweep_region.lower().replace("baselined", ""), (
        "the startup sweep must not ack -- receipt is not consumption"
    )

def test_warn_also_writes_to_a_file_not_only_stderr():
    """stderr alone is fail-SILENT for the process that matters.

    The scheduled task runs under ``pythonw.exe``, which has no console: its
    stderr goes to a handle nobody reads. That is exactly how the 2026-09-12
    outage stayed invisible for an hour -- the failure was being "reported" the
    whole time, into the void. A durable file is the half a human (or a later
    session) can actually read.

    Verified by execution while writing this: running the waiter under a broken
    interpreter wrote timestamped diagnoses to ~/.m3/logs/notification_waiter.log,
    and a healthy run wrote NOTHING -- loud on failure, silent otherwise.
    """
    src = _SRC.read_text(encoding="utf-8")
    assert "def _log_path(" in src, "no log-file resolution helper"
    warn = src[src.index("def _warn("):src.index("def _m3_admin(")]
    assert "_log_path()" in warn, "_warn must write to the log file, not only stderr"
    assert "file=sys.stderr" in warn, "_warn must ALSO keep stderr for interactive runs"


def test_logging_never_takes_down_the_waiter():
    """Best-effort by construction: a full disk, a locked file or an unwritable
    root must not kill the detector. Losing the log is strictly better than
    losing the thing being logged."""
    src = _SRC.read_text(encoding="utf-8")
    warn = src[src.index("def _log_path("):src.index("def _m3_admin(")]
    assert warn.count("except Exception") >= 2, (
        "_log_path and the write must each swallow their own failure"
    )
    assert "return None" in warn, "_log_path must degrade to None, not raise"


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
