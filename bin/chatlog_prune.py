#!/usr/bin/env python3
"""chatlog_prune — aged noise pruning for chatlog turns.

Builds on bin/chatlog_decay.py's philosophy (deterministic, age-graded) but
adds (a) a PRUNE tier that soft-deletes aged noise, (b) a repeated-status
request/response detector, and (c) an aged generic-low-value classifier.

THREE AGE TIERS (all tunable):
    age < FRESH_DAYS            -> keep untouched (recent noise may still have value)
    FRESH_DAYS <= age < PRUNE   -> DECAY: lower importance + set valid_to (suppress)
    age >= PRUNE_DAYS           -> PRUNE: soft-delete (is_deleted=1) so it leaves
                                   retrieval and propagates fleet-wide as a tombstone

Soft-delete only: nothing is hard-deleted or VACUUMed here, so it stays
recoverable and rides the normal delta sync (is_deleted + updated_at bump).

NOISE = ephemeral (PIDs/UUIDs/status/tmp/JSON one-liners)
      | short user command (<=4 words, not a question/refusal)
      | repeated-status (normalized content recurs >= STATUS_MIN_CLUSTER times
        AND matches status request/response vocabulary)  [request AND response]
      | generic-low (importance <= GENERIC_IMP_MAX, unpromoted chat_log,
        not a question, no strong-signal markers)         [the bulk]

KEEP guards (never noise): questions ('?'), explicit refusals, importance above
a floor, assistant turns carrying decision/code markers.

USAGE
    python3 chatlog_prune.py --db <path> [--dry-run]            # default: dry-run
    python3 chatlog_prune.py --db <path> --apply
    options: --fresh-days 14 --prune-days 45 --status-min-cluster 5
             --generic-imp-max 0.3 --no-generic
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any

# ── Noise patterns (ported from chatlog_decay.py + extensions) ────────────────
_PID_OR_UUID_RE = re.compile(
    r"(?:\bPID\s*\d+|\bprocess[_\s]+id[\s:=]*\d+|\bport\s*\d{4,5}"
    r"|\bbatches/[A-Za-z0-9_-]{12,}"
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
    r"|/tmp/[A-Za-z0-9._/-]+|AppData[\\/]+Local[\\/]+Temp)",
    re.IGNORECASE,
)
_STATUS_RE = re.compile(
    r"\b(?:completion[:\s]+\d+\.?\d*\s*%|cost[:\s]+\$?\d+\.\d+"
    r"|\d+\s*/\s*\d+\s+(?:in_?progress|done|pending|sessions)"
    r"|workers?\s+alive[:\s]+\d+|slice\s+\d+\s+poll\s*#?\d+)",
    re.IGNORECASE,
)
_SHORT_COMMAND_WORDS = frozenset({
    "status", "start", "stop", "go", "yes", "y", "ok", "proceed", "continue",
    "do it", "kick off", "run it", "fire", "fire it",
})
_REFUSAL_WORDS = frozenset({"no", "stop", "kill", "abort", "wait", "hold"})
# status request/response vocabulary for the repeated-status detector
_STATUS_REQ_RE = re.compile(
    r"\b(status|progress|any\s+update|update\??|how('?s| is)\s+it\s+going"
    r"|where\s+are\s+we|eta|done\s*\??|finished\s*\??|ready\s*\??)\b", re.I)
_STATUS_RESP_RE = re.compile(
    r"(\bstatus\b|\bprogress\b|\d+\s*%|\d+\s*/\s*\d+|\bremaining\b|\bso far\b"
    r"|\bin[_\s]?progress\b|\bcompleted?\b|\bpending\b|\bworkers?\b|\beta\b)", re.I)
# strong-signal markers that protect a generic turn from being pruned
_SIGNAL_RE = re.compile(
    r"```|\bdecid(e|ed|ing|sion)\b|\bbecause\b|\btherefore\b|\bremember\b"
    r"|\bnever\b|\balways\b|\bIMPORTANT\b|\bnote that\b|\bgotcha\b|\bTODO\b", re.I)

# Durable-knowledge signals (same family used by the promotion scan). A turn
# carrying any of these is PROTECTED from soft-delete: it may be suppressed
# (importance lowered) when aged, but never tombstoned — so unpromoted value
# survives. These are the tightened guards.
_DURABLE_RE = re.compile(
    r"\b(we (decided|chose|went with|will use)|decision:|settled on|going with"
    r"|the plan is to|production footgun|the rule is|canonical (form|way) is"
    r"|from now on|going forward|always (use|do|prefer)|never (use|do)"
    r"|by default|prefer \w+ (over|to)|hostname is|runs on port \d|listening on \d"
    r"|configured (to|with)|the (endpoint|url|path|model|db|database) (is|=|:)"
    r"|installed (at|in)|API key|lives (at|in) /|stored (at|in))\b", re.I)
_STRUCT_RE = re.compile(r"(^|\n)\s*(#{1,3}\s|\*\s|-\s|\d+[.)]\s)", re.M)

# Noise categories that are ALWAYS safe to soft-delete (unambiguous chatter).
_HARD_NOISE = frozenset({"short_cmd", "repeat_status_req", "repeat_status_resp"})


def _is_protected(content: str, generic_protect_len: int) -> bool:
    """A turn worth keeping out of the tombstone bin (suppress-only)."""
    if not content:
        return False
    if _DURABLE_RE.search(content) or _SIGNAL_RE.search(content):
        return True
    # substantial, structured explanation = likely reference value
    if len(content) >= generic_protect_len and len(_STRUCT_RE.findall(content)) >= 2:
        return True
    return False


def _norm_key(content: str) -> str:
    """Normalized form for clustering: strip volatile tokens, lowercase, head."""
    s = content[:400].lower()
    s = _PID_OR_UUID_RE.sub(" ", s)
    s = re.sub(r"[0-9a-f]{6,}", " ", s)          # hex blobs / hashes
    s = re.sub(r"\d+", " ", s)                    # all digits
    s = re.sub(r"[^a-z ]+", " ", s)               # punctuation
    s = re.sub(r"\s+", " ", s).strip()
    return s[:120]


def _is_general_ephemeral(content: str) -> bool:
    if not content:
        return False
    snip = content[:2000]
    if _PID_OR_UUID_RE.search(snip) or _STATUS_RE.search(snip):
        return True
    stripped = snip.strip()
    if len(stripped) <= 30 and (stripped.startswith("{") or stripped.lstrip("-+").isdigit()):
        return True
    return False


def _is_short_user_command(role: str, content: str) -> bool:
    if (role or "").lower() != "user" or not content:
        return False
    s = content.strip().lower()
    if "?" in s or any(w in s.split() for w in _REFUSAL_WORDS):
        return False
    return len(s.split()) <= 4 or s in _SHORT_COMMAND_WORDS


def _age_days(created_at: str | None, now_ts: float) -> float:
    if not created_at:
        return 0.0
    s = created_at.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s.split(".")[0], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except Exception:
            return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now_ts - dt.timestamp()) / 86400.0)


def _has_column(conn, dialect, table, col) -> bool:
    sql, params = dialect.columns_of(table)
    return any(r[0] == col for r in conn.execute(sql, params).fetchall())


def classify(role: str, content: str, importance: float, norm: str,
             cluster_size: int, args) -> tuple[str | None, str]:
    """Return (noise_category | None, reason). None => keep."""
    # KEEP guards first
    if importance is not None and importance > args.keep_imp_floor:
        return None, "keep:importance"
    if content and "?" in content[:200]:
        return None, "keep:question"
    if _SIGNAL_RE.search(content or ""):
        return None, "keep:signal"
    # NOISE classes
    if _is_general_ephemeral(content):
        return "ephemeral", "pattern"
    if _is_short_user_command(role, content):
        return "short_cmd", "short"
    # repeated status: recurring normalized form + status vocabulary, both sides
    if cluster_size >= args.status_min_cluster and norm:
        r = (role or "").lower()
        if r == "user" and _STATUS_REQ_RE.search(content or ""):
            return "repeat_status_req", f"cluster={cluster_size}"
        if r in ("assistant", "tool", "") and _STATUS_RESP_RE.search(content or ""):
            return "repeat_status_resp", f"cluster={cluster_size}"
    # generic low-value (the bulk) — aged unpromoted default-importance chat
    if not args.no_generic and (importance is None or importance <= args.generic_imp_max):
        return "generic_low", "low-imp-unpromoted"
    return None, "keep:default"


def run(db_path: str, args) -> dict:
    if not os.path.exists(db_path):
        return {"error": f"DB not found: {db_path}"}
    now_ts = time.time()
    S: dict[str, Any] = {
        "db": db_path, "apply": args.apply, "scanned": 0,
        "fresh_kept": 0, "decay": {}, "prune": {}, "kept_noise_recent": 0,
        "writes_decay": 0, "writes_prune": 0,
        "prune_rows": 0, "prune_content_mb": 0.0, "errors": [],
        "params": {"fresh_days": args.fresh_days, "prune_days": args.prune_days,
                   "status_min_cluster": args.status_min_cluster,
                   "generic_imp_max": args.generic_imp_max, "generic": not args.no_generic},
    }
    # Backend-aware. On SQLite: connect directly to the caller's chatlog file
    # (byte-identical to the original) — the core table name, no path indirection
    # (get_chatlog_conn's own resolver could point at a DIFFERENT chatlog path
    # than the db_path the caller passed). On PG: one DB, chatlog isolated by the
    # chat_log_* table name via the backend pool, hitting the LIVE store.
    from memory.backends import active_backend, chatlog_table

    backend = active_backend()
    _d = backend.dialect()
    _p = _d.param()
    if backend.name == "sqlite":
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row  # name-based row access (r["content"] etc.)
        try:
            return _run_sweep(conn, db_path, args, now_ts, S, _d, _p, "memory_items", True)
        finally:
            conn.close()
    else:
        _tbl = chatlog_table("items")
        with backend.connection() as conn:
            return _run_sweep(conn, db_path, args, now_ts, S, _d, _p, _tbl, False)


def _run_sweep(conn, db_path, args, now_ts, S, _d, _p, _tbl, _is_sqlite) -> dict:
    # §10 DB hygiene: tune the connection (WAL autocheckpoint, journal_size_limit,
    # mmap/cache) so an --apply run doesn't bloat the chatlog WAL. Best-effort —
    # a missing helper or odd path must not abort a prune. SQLite-only pragmas.
    checkpoint_truncate: "Any" = None  # populated from sqlite_pragmas below (SQLite only)
    if _is_sqlite:
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from sqlite_pragmas import apply_pragmas, checkpoint_truncate, profile_for_db
            apply_pragmas(conn, profile_for_db(db_path))
        except Exception:
            checkpoint_truncate = None  # type: ignore[assignment]
    try:
        for col in ("id", "type", "title", "content", "importance", "created_at", "is_deleted"):
            if not _has_column(conn, _d, _tbl, col):
                return {"error": f"{_tbl}.{col} missing"}
        has_valid_to = _has_column(conn, _d, _tbl, "valid_to")
        role_sql = """CASE WHEN title LIKE 'user@%' THEN 'user'
                           WHEN title LIKE 'assistant@%' THEN 'assistant'
                           WHEN title LIKE 'system@%' THEN 'system'
                           WHEN title LIKE 'tool@%' THEN 'tool' ELSE '' END"""
        # §4/§8: scope the scan to the ACTIONABLE window. Only rows aged past
        # fresh_days are ever decayed/pruned (fresher noise is kept, see below),
        # so there's no reason to pull the whole chat_log table into Python every
        # run — that turns a large backlog into one unbounded monster pass. Filter
        # by created_at (ISO-8601 sorts lexically) so the scan and the in-Python
        # cluster build stay bounded to what we might act on. `created_at` is
        # indexed on the chatlog DB; a missing index just degrades to a scan of
        # the same rows we'd have read anyway.
        fresh_cutoff = datetime.fromtimestamp(
            now_ts - args.fresh_days * 86400.0, timezone.utc
        ).isoformat()
        rows = conn.execute(f"""SELECT id, {role_sql} AS role, content, importance, created_at
                                FROM {_tbl}
                                WHERE type='chat_log' AND is_deleted=0
                                  AND created_at < {_p}
                                ORDER BY created_at ASC""", (fresh_cutoff,)).fetchall()
        # PASS 1: build normalized-content cluster sizes (for repeat-status)
        cluster: dict[str, int] = {}
        norms: dict[str, str] = {}
        for r in rows:
            n = _norm_key(r["content"] or "")
            norms[r["id"]] = n
            if n:
                cluster[n] = cluster.get(n, 0) + 1
        # PASS 2: classify + decide by age tier. §8: bound the WRITE volume per
        # run via max_actions so a huge backlog drains across cycles instead of
        # one monster pass (rows are oldest-first, so we always make forward
        # progress on the most-aged noise). max_actions <= 0 means "no cap".
        max_actions = int(getattr(args, "max_actions", 0) or 0)
        S["capped"] = False
        decay_buf: list[tuple[Any, float]] = []
        prune_buf: list[tuple[Any]] = []
        for r in rows:
            S["scanned"] += 1
            role = r["role"] or ""
            content = r["content"] or ""
            imp = float(r["importance"]) if r["importance"] is not None else 0.3
            age = _age_days(r["created_at"], now_ts)
            cat, _ = classify(role, content, imp, norms[r["id"]], cluster.get(norms[r["id"]], 0), args)
            if cat is None:
                continue
            if age < args.fresh_days:
                S["kept_noise_recent"] += 1          # noise but recent -> keep
                continue
            if max_actions and (len(decay_buf) + len(prune_buf)) >= max_actions:
                # Hit the per-run action cap. Stop accumulating; the remaining
                # aged noise is handled next run. Surfaced (not silent) so a
                # standing backlog is visible.
                S["capped"] = True
                break
            # Tightened guard: protected turns (durable signal, or substantial &
            # structured, or a generic turn longer than the trivia cutoff) are
            # never tombstoned — at most suppressed. Hard-noise bypasses this.
            protected = (cat not in _HARD_NOISE) and (
                _is_protected(content, args.generic_protect_len)
                or (cat == "generic_low" and len(content) >= args.generic_delete_maxlen))
            if age < args.prune_days or protected:
                # DECAY/SUPPRESS tier
                tag = cat if age < args.prune_days else f"{cat}!protected"
                S["decay"][tag] = S["decay"].get(tag, 0) + 1
                new_imp = round(min(imp, 0.3) * 0.2, 4)
                decay_buf.append((r["id"], new_imp))
            else:
                # PRUNE tier: soft-delete (high-confidence noise only)
                S["prune"][cat] = S["prune"].get(cat, 0) + 1
                S["prune_rows"] += 1
                S["prune_content_mb"] += len(content) / 1048576.0
                prune_buf.append((r["id"],))
        S["prune_content_mb"] = round(S["prune_content_mb"], 1)
        if args.apply:
            ts = datetime.now(timezone.utc).isoformat()
            if _is_sqlite:
                conn.execute("BEGIN IMMEDIATE")
            if has_valid_to:
                conn.executemany(
                    f"UPDATE {_tbl} SET importance={_p}, valid_to={_p}, updated_at={_p} "
                    f"WHERE id={_p} AND type='chat_log'",
                    [(ni, ts, ts, rid) for rid, ni in decay_buf])
            else:
                conn.executemany(
                    f"UPDATE {_tbl} SET importance={_p}, updated_at={_p} WHERE id={_p} AND type='chat_log'",
                    [(ni, ts, rid) for rid, ni in decay_buf])
            conn.executemany(
                f"UPDATE {_tbl} SET is_deleted=1, updated_at={_p} WHERE id={_p} AND type='chat_log'",
                [(ts, rid) for (rid,) in prune_buf])
            conn.commit()
            S["writes_decay"], S["writes_prune"] = len(decay_buf), len(prune_buf)
            # §10: truncate the WAL after a write batch so it doesn't grow unbounded.
            if checkpoint_truncate is not None:
                try:
                    checkpoint_truncate(conn)
                except Exception as exc:
                    S["errors"].append(f"wal_checkpoint: {exc!r}")
    except Exception as exc:
        conn.rollback(); S["errors"].append(repr(exc))
    return S


def main() -> int:
    ap = argparse.ArgumentParser(description="Aged noise pruning for chatlog.")
    ap.add_argument("--db", required=True)
    ap.add_argument("--fresh-days", type=float, default=14.0)
    ap.add_argument("--prune-days", type=float, default=45.0)
    ap.add_argument("--status-min-cluster", type=int, default=5)
    ap.add_argument("--generic-imp-max", type=float, default=0.3)
    ap.add_argument("--keep-imp-floor", type=float, default=0.4)
    ap.add_argument("--generic-protect-len", type=int, default=300,
                    help="generic turns >= this length (if structured) are suppress-only")
    ap.add_argument("--generic-delete-maxlen", type=int, default=300,
                    help="generic turns >= this length are never tombstoned (suppress-only)")
    ap.add_argument("--no-generic", action="store_true")
    ap.add_argument("--max-actions", type=int, default=0,
                    help="Max decay+prune writes per run (0 = no cap). Bounds a "
                         "single pass so a large backlog drains across runs "
                         "instead of one monster pass; oldest noise goes first.")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.apply and args.dry_run:
        print("error: --apply and --dry-run are mutually exclusive", file=sys.stderr); return 2
    import json
    print(json.dumps(run(args.db, args), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
