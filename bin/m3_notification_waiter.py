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
import subprocess
import sys
import time


def wal_fingerprint(wal: pathlib.Path) -> tuple:
    """(mtime, size) or () when absent. Compare BOTH: checkpointing can shrink
    the file, so size alone both misses growth-then-truncate and false-fires."""
    try:
        st = wal.stat()
        return (st.st_mtime, st.st_size)
    except FileNotFoundError:
        return ()


def unread_ids(agent_id: str) -> set:
    """Ask m3 what is actually unread. A WAL change means SOMETHING was written
    -- a chatlog turn, an embedding -- not necessarily a notification for us, so
    every wake must be confirmed here or the waiter fires constantly."""
    try:
        out = subprocess.run(
            ["m3", "admin", "notifications_poll", "--agent_id", agent_id, "--limit", "50"],
            capture_output=True, text=True, timeout=90,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if out.returncode != 0:
        return set()
    import re
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
            received = {}
            for a in hits:
                try:
                    r = subprocess.run(  # nosec B603 - argv list, no shell
                        ["m3", "admin", "notifications_mark_received",
                         "--agent_id", a, "--yes"],
                        capture_output=True, text=True, timeout=90,
                    )
                    received[a] = (r.returncode == 0)
                except (OSError, subprocess.TimeoutExpired):
                    received[a] = False

            acked = {}
            if args.ack:
                for a in hits:
                    try:
                        r = subprocess.run(  # nosec B603 - argv list, no shell
                            ["m3", "admin", "notifications_ack_all",
                             "--agent_id", a, "--yes"],
                            capture_output=True, text=True, timeout=90,
                        )
                        acked[a] = (r.returncode == 0)
                    except (OSError, subprocess.TimeoutExpired):
                        acked[a] = False
            print(json.dumps({"new_notifications": hits,
                              "received": received, "acked": acked}))
            return 0                      # exit == your runtime's wake signal

    print(json.dumps({"timeout": True, "agent_ids": args.agent_ids}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
