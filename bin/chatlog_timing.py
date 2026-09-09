"""Per-turn wall-clock timing from the chatlog — who spent the time, objectively.

WHY THIS EXISTS
---------------
"Why did that memory write take 30 seconds?" was answered wrong four times in a
row on 2026-09-09 (blamed m3's write path, then payload size, then line count,
then character count) because every answer was an INFERENCE. The chatlog already
stores what settles it: every turn with an ISO-8601 millisecond ``created_at``,
its role, and its length. The gap between consecutive turns IS the wall clock
for the turn that follows. Measured, not guessed.

The measurement that mattered: m3's own write is 0.09-0.51s and a full MCP stdio
round trip including cold start is 0.99s, so a 30s "save" is ~29s of AGENT
generation. This tool makes that decomposition a one-liner instead of a 15-line
ad-hoc script written under pressure.

CONCURRENCY — why this scopes by conversation, not by time
----------------------------------------------------------
Another agent (or another session on this machine) may be writing to m3 at the
same time. A pure time-window query would interleave their turns with yours and
silently corrupt every gap. So the default scope is a single
``conversation_id``, which is stable for a whole session (measured: 1,697 turns
under one id), and ``--agent`` / ``--model`` narrow further. ``idx_mi_conversation_composite``
already covers this shape, so it stays cheap as the store grows.

Reads go through ``M3Context.get_chatlog_conn()`` — the seam decides WHICH store
holds the turns (integrated / separate / hybrid) and WHICH backend serves it, so
this works on SQLite and PostgreSQL alike and on all three OSes. SELECT-only: a
concurrent writer is never blocked, and a row arriving mid-read is simply not
seen, which is correct — a partial tail beats a lock.

    python bin/chatlog_timing.py --last 30
    python bin/chatlog_timing.py --since 2026-09-09T02:40 --slowest 10
    python bin/chatlog_timing.py --conversation <uuid> --json
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys


# Locate the m3 payload's bin/ so the seam imports resolve. When this file
# lives IN the repo (bin/chatlog_timing.py) its own directory is the answer;
# when it is parked elsewhere (e.g. ~/.m3-private/perf/) fall back to the
# installed payload, then to $M3_PATH_BIN. Without this the seam imports fail
# with ModuleNotFoundError: m3_core the moment the file is moved.
def _find_bin() -> "str | None":
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [here,
             os.environ.get("M3_PATH_BIN") or "",
             os.path.expanduser("~/.m3-dev/m3-memory/bin"),
             os.path.expanduser(
                 "~/pipx/venvs/m3-memory/Lib/site-packages/m3_memory/bin")]
    for c in cands:
        if c and os.path.isfile(os.path.join(c, "memory_bridge.py")):
            return c
    return None


_BIN = _find_bin()
if _BIN and _BIN not in sys.path:
    sys.path.insert(0, _BIN)
elif not _BIN:
    raise SystemExit(
        "[X] cannot locate the m3 payload bin/ (looked next to this file, "
        "$M3_PATH_BIN, ~/.m3-dev/m3-memory/bin, and the pipx payload).")


def _query(sql_tail: str, params: "list") -> "list[tuple]":
    """Run one read against the CHATLOG store through the seam.

    Everything backend- and topology-specific is asked, never assumed:

    * ``M3Context.get_chatlog_conn()`` resolves WHICH store holds the turns —
      integrated (chatlog == main), separate, or hybrid. Do not compute a path.
    * ``chatlog_table("items")`` resolves the TABLE NAME per backend
      (``memory_items`` on SQLite, ``chat_log_items`` on PostgreSQL and every
      other SQL backend).
    * ``dialect().placeholder(1)`` renders ``?`` vs ``%s``.
    * ``dialect().byte_length(col)`` for portable length (a bare ``LENGTH()`` on
      ``content`` is what the SQL-drift guard flags, and it means different
      things per backend).

    The first draft of this file hardcoded ``engine_root/agent_chatlog.db`` and
    opened it with raw ``sqlite3`` — which is a §10a violation (business logic
    speaking dialect), reintroduces the raw-connection defect Finding L closed,
    breaks outright on PostgreSQL, and gets the UNIFIED topology wrong. It also
    hit the documented false emergency in the other direction: query the MAIN
    store on a split deployment and you get ZERO rows, which reads as "capture
    is dead" while capture is perfectly healthy.
    """
    from m3_core.context import M3Context
    from memory.backends import active_backend, chatlog_table

    T = chatlog_table("items")
    p = active_backend().dialect().placeholder(1)
    sql = sql_tail.format(T=T, p=p)
    ctx = M3Context.for_db(None)
    with ctx.get_chatlog_conn() as conn:
        cur = conn.execute(sql, tuple(params))
        return list(cur.fetchall())


def _rows(args) -> "list[dict]":
    """Turns in scope, oldest first."""
    from memory.backends import active_backend
    p = active_backend().dialect().placeholder(1)

    where, params = ["type = 'chat_log'"], []
    for col, val in (("conversation_id", args.conversation), ("agent_id", args.agent),
                     ("model_id", args.model)):
        if val:
            where.append(f"{col} = {p}")
            params.append(val)
    if args.since:
        where.append(f"created_at >= {p}")
        params.append(args.since)
    params.append(args.last)

    # Length via the seam's byte_length primitive, never a bare LENGTH() —
    # `bare_length_on_content` is exactly what the SQL-drift guard flags, and
    # LENGTH() means different things per backend.
    clen = active_backend().dialect().byte_length("content")
    got = _query(
        f"SELECT created_at, title, agent_id, model_id, {clen} "
        "FROM {T} WHERE " + " AND ".join(where) +
        " ORDER BY created_at DESC LIMIT {p}",
        params,
    )
    return [
        {"created_at": str(r[0]), "title": r[1] or "", "agent_id": r[2] or "",
         "model_id": r[3] or "", "chars": r[4] or 0}
        for r in reversed(got)          # DESC+LIMIT takes the TAIL, then re-sort ASC
    ]


def _annotate(rows: "list[dict]") -> "list[dict]":
    """Attach gap_s (wall clock attributable to this turn) and role."""
    prev = None
    for r in rows:
        t = _dt.datetime.fromisoformat(r["created_at"].replace("Z", "+00:00"))
        r["gap_s"] = round((t - prev).total_seconds(), 1) if prev else None
        r["role"] = "assistant" if r["title"].startswith("assistant") else "user"
        # chars/sec is the useful ratio: a LOW rate on a long turn means time
        # went somewhere other than visible output — tool calls, or an Edit
        # whose old_string had to be reproduced verbatim. Both are generated
        # tokens that never appear in the reply.
        r["ch_per_s"] = (round(r["chars"] / r["gap_s"]) if r["gap_s"] else None)
        prev = t
    return rows


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conversation", default="", help="conversation_id to scope to "
                    "(default: the most recent one — see the concurrency note)")
    ap.add_argument("--since", default="", help="ISO timestamp lower bound")
    ap.add_argument("--last", type=int, default=40, help="max turns (default 40)")
    ap.add_argument("--agent", default="", help="filter by agent_id")
    ap.add_argument("--model", default="", help="filter by model_id")
    ap.add_argument("--slowest", type=int, default=0,
                    help="also print the N slowest turns")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    if not args.conversation:
        # Default to the MOST RECENT conversation rather than a bare time window,
        # so a concurrent agent's turns can never interleave into the gaps.
        # Through the seam like every other read here — no path, no raw driver.
        got = _query(
            "SELECT conversation_id FROM {T} "
            "WHERE type='chat_log' AND conversation_id IS NOT NULL "
            "ORDER BY created_at DESC LIMIT {p}", [1])
        if got:
            args.conversation = got[0][0]

    rows = _annotate(_rows(args))
    if not rows:
        print("no turns in scope")
        return 0

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    print(f"conversation {args.conversation or '(any)'} — {len(rows)} turns")
    print(f"{'time':13} {'gap':>8} {'role':9} {'chars':>7} {'ch/s':>6}  title")
    for r in rows:
        gap = f"{r['gap_s']:.1f}s" if r["gap_s"] is not None else "-"
        rate = f"{r['ch_per_s']}" if r["ch_per_s"] else "-"
        print(f"{r['created_at'][11:23]} {gap:>8} {r['role']:9} "
              f"{r['chars']:>7} {rate:>6}  {r['title'][:52]}")

    # Attribution: the whole point is separating agent time from everything else.
    asst = [r for r in rows if r["role"] == "assistant" and r["gap_s"]]
    total = sum(r["gap_s"] for r in rows if r["gap_s"])
    a_time = sum(r["gap_s"] for r in asst)
    print()
    print(f"span {total:.0f}s · assistant turns {a_time:.0f}s "
          f"({100*a_time/total:.0f}%) across {len(asst)} turns · "
          f"{sum(r['chars'] for r in asst):,} chars")

    if args.slowest:
        print(f"\nslowest {args.slowest}:")
        for r in sorted(asst, key=lambda x: -x["gap_s"])[: args.slowest]:
            print(f"  {r['gap_s']:6.1f}s  {r['chars']:>6}ch  "
                  f"{r['ch_per_s'] or '-':>5} ch/s  {r['title'][:48]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
