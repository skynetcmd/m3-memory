#!/usr/bin/env python3
"""Single-shot m3 notification waiter — blocks until YOUR inbox has something new.

Exits 0 the moment a new notification appears for --agent-id. Exits 2 on timeout.
Run it as a background/async task: your runtime's process-completion signal is
what pushes the wake-up turn to you, so this costs ZERO conversation turns while
it waits.

WHY IT WATCHES THE WAL FILE, NOT THE DATABASE
SQLite has no cross-process blocking change notification. Update hooks are
in-process only and fire solely for the connection's OWN writes, so a waiter
CANNOT be woken by another process inserting a row (verified, sqlite 3.50.4).
Watching agent_memory.db-wal is the working substitute: a notify write changes
both its mtime and its size (measured: 263712 -> 280192 bytes).

The cheap 1s polling happens HERE, in a subprocess that costs no context. That
is the whole point: on-change delivery to the agent, without a turn per tick.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from datetime import datetime, timezone


def wal_fingerprint(wal: pathlib.Path) -> tuple:
    """(mtime, size) or () when absent. Compare BOTH: checkpointing can shrink
    the file, so size alone both misses growth-then-truncate and false-fires."""
    try:
        st = wal.stat()
        return (st.st_mtime, st.st_size)
    except FileNotFoundError:
        return ()


def _no_window() -> dict:
    """Windows: hide the console window without detaching the child's stdio.

    STARTUPINFO/SW_HIDE, deliberately NOT creationflags=CREATE_NO_WINDOW.
    Measured 2026-09-12 in a real console: CREATE_NO_WINDOW suppresses the
    child's INHERITED stdout as well as the window, so a failing subprocess
    reports nothing at all. SW_HIDE hides the window and keeps the pipes.
    Single owner so the three call sites below cannot drift apart.
    """
    if sys.platform != "win32":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": si}


def _log_path() -> pathlib.Path | None:
    """~/.m3/logs/notification_waiter.log, honouring M3_ENGINE_ROOT's parent.

    Resolution mirrors the engine-root rules rather than hardcoding ~/.m3, so a
    machine that relocated its roots still gets its log next to the others.
    Returns None rather than raising: logging is best-effort and must never take
    down the thing it is observing.
    """
    try:
        engine = os.environ.get("M3_ENGINE_ROOT")
        base = pathlib.Path(engine).parent if engine else pathlib.Path.home() / ".m3"
        d = base / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d / "notification_waiter.log"
    except Exception:
        return None


def _warn(msg: str) -> None:
    """One place that reports a subprocess failure. Writes to stderr AND a file.

    This function is the fix for the outage of 2026-09-12. `unread_ids` used to
    swallow every failure into `return set()`, which is INDISTINGUISHABLE from
    "the inbox is empty". The waiter then ran for an hour detecting nothing,
    through three separate probes from another agent, while Task Scheduler
    reported Running and every health check we had reported OK.

    The root cause when it finally surfaced was trivial -- the task launched an
    interpreter that could not `import m3_memory` unless the working directory
    happened to be the source checkout, and the task has no WorkingDirectory.
    One line of stderr would have found it in seconds.

    THE FILE IS NOT REDUNDANT WITH STDERR. The scheduled task runs under
    pythonw.exe, which has no console: stderr is written to a handle nobody
    reads. A "fail loud" path whose only output goes into the void is still
    fail-silent for the exact process this feature exists to supervise -- which
    is precisely how the outage above stayed invisible. The file is the half
    that a human or a later session can actually read.

    DESIGN_PHILOSOPHIES section 3: fail loud. A detector that cannot tell you it
    is broken is worse than no detector, because it also consumes the attention
    you would have spent noticing.
    """
    line = f"[waiter] {msg}"
    print(line, file=sys.stderr, flush=True)
    p = _log_path()
    if p is None:
        return
    try:
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {line}\n")
    except Exception:
        # Best-effort. A full disk or a locked file must not kill the waiter:
        # stderr already carried the message, and losing the log is strictly
        # better than losing the detector.
        pass


def _m3_admin(tool: str, agent_id: str) -> bool:
    """Run one `m3 admin <tool> --agent_id <id> --yes`. True iff it succeeded.

    Single owner for the three things every one of these calls must get right,
    each of which was wrong somewhere in this file on 2026-09-12:

    * the INTERPRETER -- `sys.executable`, never a bare ``m3`` on PATH and never
      a hardcoded absolute path. A bare ``m3`` resolves, but without
      M3_ENGINE_ROOT in the environment it targets a DIFFERENT store and starts
      migrating it from v000 while returning rc 0. A hardcoded path bakes one
      machine's home directory into a shipped file.
    * SW_HIDE, via the one helper, so a background service never flashes a
      console.
    * FAILING LOUD. `received[a] = (r.returncode == 0)` recorded a clean False
      for an hour while nothing worked.

    rc 0 is necessary but NOT sufficient, hence the stderr check: the wrong-root
    case above exits 0 having done something entirely different from what was
    asked.
    """
    try:
        r = subprocess.run(  # nosec B603 - argv list, no shell
            [sys.executable, "-m", "m3_memory.cli", "admin", tool,
             "--agent_id", agent_id, "--yes"],
            capture_output=True, text=True, timeout=90,
            **_no_window(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _warn(f"{tool} for {agent_id!r} did not run: {exc!r}")
        return False
    if r.returncode != 0:
        _warn(
            f"{tool} for {agent_id!r} exited {r.returncode} using "
            f"{sys.executable!r}. stderr: {(r.stderr or '').strip()[:400]}"
        )
        return False
    return True


def unread_ids(agent_id: str) -> set:
    """Ask m3 what is actually unread. A WAL change means SOMETHING was written
    -- a chatlog turn, an embedding -- not necessarily a notification for us, so
    every wake must be confirmed here or the waiter fires constantly.

    Returns an empty set on failure, but never SILENTLY: see _warn above.
    """
    try:
        out = subprocess.run(  # nosec B603 - argv list, no shell
            [sys.executable, "-m", "m3_memory.cli", "admin", "notifications_poll",
             "--agent_id", agent_id, "--limit", "50"],
            capture_output=True, text=True, timeout=90,
            **_no_window(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _warn(f"notifications_poll for {agent_id!r} did not run: {exc!r}")
        return set()
    if out.returncode != 0:
        # The whole point of this branch. Print the interpreter too: the failure
        # that hid for an hour was a WRONG INTERPRETER, and the message alone
        # ("No module named 'm3_memory'") does not say which python said it.
        _warn(
            f"notifications_poll for {agent_id!r} exited {out.returncode} "
            f"using {sys.executable!r} -- detection is BLIND for this agent. "
            f"stderr: {(out.stderr or '').strip()[:400]}"
        )
        return set()
    return set(re.findall(r"\[(\d+)\]", out.stdout))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent-id", required=True,
                    help="Inbox to watch. Repeatable: pass once per agent to serve several "
                         "inboxes from ONE process. The WAL trigger is shared -- a single file "
                         "change covers every inbox -- so N agents cost N cheap confirm-polls "
                         "after a change, not N watchers.",
                    action="append", dest="agent_ids")
    ap.add_argument("--engine-root", default=os.environ.get("M3_ENGINE_ROOT") or
                    str(pathlib.Path.home() / ".m3" / "engine"))
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=3600.0,
                    help="Give up after N seconds so a forgotten waiter cannot leak.")
    ap.add_argument("--supervise", action="store_true",
                    help="Never exit: after each detection, re-arm and keep waiting. For an "
                         "ONSTART scheduled task, which needs a long-lived process. Without it "
                         "the waiter is single-shot, which is what a runtime wants when the "
                         "process EXIT is the wake signal.")
    ap.add_argument("--ack", action="store_true",
                    help="Ack on detection. OFF BY DEFAULT, deliberately: the notifications table "
                         "has only `read_at` -- no separate 'received' column -- so acking here "
                         "destroys the only record that a message was unread. Measured on this "
                         "machine: 29 of 30 recent notifications carried NO task_id, so 'the task "
                         "state machine tracks it' is false for ~97 percent of real traffic. Enable this "
                         "only where every watched kind is backed by a task whose own state "
                         "survives the ack.")
    args = ap.parse_args()

    # Supervise mode loops the single-shot body instead of duplicating it, so
    # there is exactly one implementation of detect-confirm-ack and the two
    # modes cannot drift.
    if args.supervise:
        while True:
            rc = _wait_once(args)
            if rc not in (0, 2):          # 0 = delivered, 2 = idle timeout
                return rc                 # a real failure must surface, not spin
    return _wait_once(args)


def _wait_once(args) -> int:
    wal = pathlib.Path(args.engine_root) / "agent_memory.db-wal"
    # STARTUP SWEEP -- stamp receipt on anything already waiting, BEFORE the
    # baseline is taken.
    #
    # The baseline below is "what is unread right now", and the loop only ever
    # reports ids that appear AFTER it. That is correct for detecting change and
    # wrong for recording RECEIPT: a notification that arrived while this process
    # was down -- a restart, a reboot, a --timeout expiry, the hour this waiter
    # spent blind on 2026-09-12 -- gets baselined away as "pre-existing" and is
    # never stamped. The SLA then under-reports silently, which is the failure
    # this whole feature exists to remove.
    #
    # notifications_mark_received targets `received_at IS NULL`, so this is
    # idempotent (a second run stamps 0 rows and preserves the FIRST receipt
    # time) and it never touches read_at -- a swept message stays UNREAD and
    # still shows up under --unread_only. Sweeping costs one call per agent at
    # startup, once.
    for _a in args.agent_ids:
        _m3_admin("notifications_mark_received", _a)

    baselines = {a: unread_ids(a) for a in args.agent_ids}
    fp = wal_fingerprint(wal)
    deadline = time.time() + args.timeout

    while time.time() < deadline:
        time.sleep(args.interval)
        cur_fp = wal_fingerprint(wal)
        if cur_fp == fp:
            continue                      # nothing written at all
        fp = cur_fp
        hits = {}
        for a in args.agent_ids:
            now = unread_ids(a)
            gained = now - baselines[a]
            if gained:
                hits[a] = sorted(gained)
            baselines[a] = now
        if hits:
            # RECEIPT vs READING -- the distinction this whole waiter turns on.
            #
            # The SLA is "acknowledge RECEIPT within 30 seconds", and receipt
            # does not need an agent turn: a plain subprocess round-trips in
            # ~430ms. So stamping it here makes receipt deterministic and
            # independent of whether any agent is free -- a busy agent delays
            # the WORK, never the RECEIPT.
            #
            # This used to be impossible to do safely. `notifications` carried
            # ONE timestamp, `read_at`, so the only way to record receipt was to
            # ack -- which marks a message read that no agent has read, and the
            # unread flag was the only record that work was still pending. The
            # usual reassurance ("the task state machine covers it") was
            # measured and is false for real traffic: 29 of 30 recent
            # notifications carried no task_id.
            #
            # Migration 045 / pg_054 split the two, so the safe option now
            # exists: `received_at` is written here, `read_at` is left for an
            # agent that has actually read the message. --unread_only still
            # filters on `read_at`, so the inbox remains the record of
            # UNPROCESSED work. Receipt is therefore the DEFAULT.
            #
            # --ack additionally marks messages READ from here. That is still
            # opt-in and still lossy in the same way it always was: use it only
            # where every watched kind is backed by a task whose own state
            # survives the ack.
            received = {a: _m3_admin("notifications_mark_received", a) for a in hits}

            acked = {}
            if args.ack:
                acked = {a: _m3_admin("notifications_ack_all", a) for a in hits}
            print(json.dumps({"new_notifications": hits,
                              "received": received, "acked": acked}))
            return 0                      # exit == your runtime's wake signal

    print(json.dumps({"timeout": True, "agent_ids": args.agent_ids}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
