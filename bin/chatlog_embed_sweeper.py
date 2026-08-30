#!/usr/bin/env python3
"""
chatlog_embed_sweeper.py — lazy embed chat log rows missing embeddings.

Runs on a schedule (default every 30 min via install_schedules.py). Picks up
rows written with embed=False, embeds in batches using memory_core._embed_many,
and drains any spill-to-disk files from the async write queue.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from glob import glob

from m3_sdk import getenv_compat

# Setup path so we can import bin/ modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chatlog_config

logger = logging.getLogger("chatlog_embed_sweeper")

# --- undrainable-spill quarantine (added 2026-08-29) -------------------------
# A spill file whose target can never exist (a deleted pytest tmpdir, a %TEMP%
# scratch DB) is refused on every pass — correctly, since discarding a turn is
# worse than keeping it (§3). But with no escape hatch the sweeper retries it
# forever: measured at 312,684 passes / ~1,230 MB/s / a 720 MB log, which
# stalled the desktop UI. Count consecutive failures per file and, past the
# budget, move it aside. MOVE, never delete — the turns must survive (§3).
_QUARANTINE_DIRNAME = "quarantine"
_SPILL_CFG_TTL = 5.0
_spill_cfg_cache: dict = {"ts": 0.0, "mtime": None, "max_attempts": None}


def _spill_config_path() -> str:
    from m3_core.paths import get_m3_config_root
    return os.path.join(get_m3_config_root(), ".spill_config.json")


def _spill_max_attempts(now: float | None = None) -> int:
    """Consecutive-failure budget before a spill file is quarantined.

    Precedence: config file > env var > default (§3). A config FILE, not an env
    var alone: the Windows scheduled task / launchd agent / systemd --user unit
    that runs the sweeper inherits a bare environment, so an env-only knob is
    absent in exactly the process that needs it. Re-read only when the file's
    mtime changes, stat throttled to _SPILL_CFG_TTL (§4). Never raises.
    """
    now = now if now is not None else time.time()
    if now - _spill_cfg_cache["ts"] >= _SPILL_CFG_TTL:
        _spill_cfg_cache["ts"] = now
        try:
            mtime = os.stat(_spill_config_path()).st_mtime
        except OSError:
            mtime = None  # absent/unreadable -> env + default
        if mtime != _spill_cfg_cache["mtime"]:
            _spill_cfg_cache["mtime"] = mtime
            cfg: dict = {}
            if mtime is not None:
                try:
                    with open(_spill_config_path(), encoding="utf-8") as fh:
                        cfg = json.load(fh) or {}
                except Exception as e:  # noqa: BLE001 — any parse failure
                    # §3 never silent: a malformed file would otherwise revert to
                    # defaults invisibly, so the operator's tuning is not live.
                    logger.warning(
                        "Spill config %s is unreadable/malformed (%s) — falling "
                        "back to env var + default until fixed.",
                        _spill_config_path(), e)
                    cfg = {}
            _spill_cfg_cache["max_attempts"] = cfg.get("max_drain_attempts")

    val = _spill_cfg_cache["max_attempts"]
    if val is not None:
        try:
            return max(1, int(val))
        except (TypeError, ValueError):
            pass
    try:
        # os.environ.get, NOT getenv_compat: that helper maps a DEPRECATED
        # legacy name to a new one, and this knob is new — it has no legacy
        # spelling to be compatible with (test_env_rename_map_drift enforces
        # that every getenv_compat call site is a real rename).
        return max(1, int(os.environ.get("M3_SPILL_MAX_ATTEMPTS", "5") or 5))
    except (TypeError, ValueError):
        return 5


def _attempts_path(spill_path: str) -> str:
    return spill_path + ".attempts"


def _read_attempts(spill_path: str) -> int:
    """Consecutive failures recorded for this spill file (0 if none/unreadable)."""
    try:
        with open(_attempts_path(spill_path), encoding="utf-8") as fh:
            return int(json.load(fh).get("consecutive_failures", 0))
    except Exception:  # noqa: BLE001 — absent or corrupt both mean "start over"
        return 0


def _record_attempt(spill_path: str, ok: bool, reason: str = "") -> int:
    """Bump (or clear) the consecutive-failure count. Returns the new count."""
    if ok:
        try:
            os.remove(_attempts_path(spill_path))
        except OSError:
            pass
        return 0
    n = _read_attempts(spill_path) + 1
    try:
        with open(_attempts_path(spill_path), "w", encoding="utf-8") as fh:
            json.dump({
                "consecutive_failures": n,
                "last_error": reason[:500],
                "last_attempt": datetime.now(timezone.utc).isoformat(),
            }, fh)
    except OSError as e:
        # Best-effort: a counter we cannot persist just means we retry longer.
        logger.warning("Could not record drain attempt for %s: %s", spill_path, e)
    return n


def _quarantine_spill(spill_path: str, reason: str, attempts: int) -> bool:
    """Move an undrainable spill file out of the hot path. True if moved.

    The turns are NOT lost (§3): they sit in <spill_dir>/quarantine/ and drain
    again if moved back once the target store exists.
    """
    try:
        qdir = os.path.join(os.path.dirname(spill_path), _QUARANTINE_DIRNAME)
        os.makedirs(qdir, exist_ok=True)
        dest = os.path.join(qdir, os.path.basename(spill_path))
        if os.path.exists(dest):  # keep both rather than clobber
            dest = "%s.%s" % (dest, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"))
        os.replace(spill_path, dest)
        try:
            os.remove(_attempts_path(spill_path))
        except OSError:
            pass
        logger.error(
            "QUARANTINED undrainable spill %s -> %s after %d consecutive failures "
            "(%s). Turns are PRESERVED, not deleted — move the file back once the "
            "target store exists. Raise max_drain_attempts in %s to retry longer.",
            spill_path, dest, attempts, reason, _spill_config_path())
        return True
    except OSError as e:
        logger.warning("Could not quarantine %s: %s", spill_path, e)
        return False


async def drain_spill(conn=None) -> int:
    """
    Drain spill JSONL files back into the chat log store(s).

    Spilled items written after the DB-parameter refactor carry the target
    path they were enqueued against (``_db_path``). We honor that per-item
    so rows from a session that pointed at a dedicated test/benchmark DB
    don't silently land in the live store. Legacy spill items without
    ``_db_path`` fall back to the live resolver.

    Reinsertion delegates to ``chatlog_core._executemany_insert`` — the SAME
    writer the live chatlog path uses — rather than a second INSERT maintained
    here (§10a: duplicated SQL is the defect; copies drift). That gets backend
    portability for free (``memory_items``/``?`` on SQLite, ``chat_log_items``/
    ``%s`` on PostgreSQL) and, critically, writes ALL 22 columns. A spill line is
    the queued item verbatim, so ``user_id``/``scope`` (tenancy — see
    ``Dialect.scope_predicates``), ``content_hash`` (dedup) and ``origin_device``
    survive the round-trip.

    Spill is NOT a SQLite-only concern — ``chatlog_core._spill_batch`` fires on
    ANY flush exception and on queue-full backpressure, regardless of backend,
    and a PG outage is precisely when spill accumulates.

    ``conn`` is accepted for backwards compatibility and ignored.

    Returns count of rows inserted across all target stores.
    """
    from memory.backends import active_backend

    spill_dir = chatlog_config.SPILL_DIR
    if not os.path.exists(spill_dir):
        return 0

    spill_files = glob(os.path.join(spill_dir, "*.jsonl"))
    if not spill_files:
        return 0

    _backend = active_backend()

    total_drained = 0
    _max_attempts = _spill_max_attempts()
    for spill_path in spill_files:
        # §4: a file that has already spent its retry budget is quarantined
        # WITHOUT reopening/reparsing it — one os.stat, not a full drain pass.
        # This is what stops a permanently-undrainable spill costing anything.
        _prior = _read_attempts(spill_path)
        if _prior >= _max_attempts:
            _quarantine_spill(spill_path, "exceeded retry budget", _prior)
            continue
        try:
            # Group rows by their captured target DB. Legacy spills (no
            # _db_path) land on the sweeper's connection under the None key.
            per_db: dict = {}
            with open(spill_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        doc = json.loads(line)
                    except json.JSONDecodeError as e:
                        logger.warning("Skipping malformed JSON in %s: %s", spill_path, e)
                        continue

                    # Normalise a spilled doc back into the shape the canonical
                    # writer expects. A spill line is the QUEUED item verbatim
                    # (`{**item, "_id", "_title", "_content", "_metadata_json",
                    # "_created_at", "_db_path"}`), so every field the live write
                    # path uses — user_id, scope, origin_device, valid_from, … —
                    # is already here; only the private keys need defaulting for
                    # legacy/hand-written lines.
                    host_agent = doc.get("host_agent", "unknown")
                    doc.setdefault("_id", str(uuid.uuid4()))
                    doc.setdefault("_content", doc.get("content", ""))
                    doc.setdefault("_title", "")
                    doc.setdefault("_created_at",
                                   doc.get("timestamp")
                                   or datetime.now(timezone.utc).isoformat())
                    doc.setdefault("conversation_id", "")
                    doc.setdefault("model_id", doc.get("model_id", "unknown"))
                    if not doc.get("agent_id"):
                        doc["agent_id"] = f"{host_agent}:spill"
                    if not doc.get("_metadata_json"):
                        doc["_metadata_json"] = json.dumps({
                            "role": doc.get("role", "unknown"),
                            "provider": doc.get("provider", "unknown"),
                            "model_id": doc.get("model_id", "unknown"),
                            "host_agent": host_agent,
                            "spill_source": True,
                        })

                    per_db.setdefault(doc.get("_db_path"), []).append(doc)

            file_drained = 0
            had_error = False
            last_error = ""
            for db_path, rows in per_db.items():
                if not rows:
                    continue
                # A captured _db_path whose PARENT DIRECTORY is gone is a stale
                # target — a deleted benchmark tree, an old engine root. Opening
                # it would silently CREATE the directory and an empty database,
                # write the turns into that orphan store, and then delete the
                # spill file: the rows are "drained" into a DB nobody reads.
                # Refuse, keep the spill file, and say so (§3 — fail loud, and
                # never discard the only copy of a turn).
                #
                # SQLite ONLY, gated on capability, never on `!= postgres` (§10a).
                # On a pooled backend `_db_path` is a LABEL, not a location
                # (§10) — a filesystem check on it is meaningless and, worse,
                # non-deterministic: os.path.abspath() resolves a bare label
                # against the CWD, so the same spill row drains from one working
                # directory and is refused from another. A DSN-shaped label
                # ("postgresql://host/db") fails the check outright and would
                # strand PG spill permanently. The orphan-store hazard is
                # inherently SQLite's — a server backend cannot be conjured by
                # connecting to it.
                if (db_path and _backend.name == "sqlite"
                        and not os.path.isdir(os.path.dirname(os.path.abspath(db_path)))):
                    logger.error(
                        "Spill target %s is stale (parent directory missing) — "
                        "refusing to create an orphan store. Keeping %s; "
                        "re-point or remove the stale rows to drain it.",
                        db_path, spill_path)
                    had_error = True
                    last_error = "stale target %s (parent directory missing)" % db_path
                    continue

                # Reuse the CANONICAL chatlog writer rather than a second INSERT
                # here (§10a — duplicated SQL is the defect; copies drift). It
                # already groups by `_db_path`, activates it, routes through
                # M3Context.get_chatlog_conn(), and writes all 22 columns via
                # the seam on either backend.
                #
                # This is not a style preference: the hand-rolled INSERT this
                # replaces wrote only 10 columns, silently dropping user_id and
                # scope (both NULL-defaulted) — so a drained turn carried NO
                # tenancy and became invisible to, or leaked across,
                # Dialect.scope_predicates filtering (§7). It also dropped
                # content_hash (breaking dedup) and origin_device (whose column
                # default is the literal 'macbook', mislabelling every drained
                # row on a Windows or Linux host). The spill file carried all of
                # it; only the reinsertion threw it away.
                try:
                    from chatlog_core import _executemany_insert
                    written = await asyncio.to_thread(_executemany_insert, rows)
                    file_drained += written
                    logger.info("Drained %d rows from %s -> %s",
                                written, spill_path, db_path or "<live resolver>")
                except Exception as e:  # noqa: BLE001 — per-target isolation
                    # Backend-agnostic: sqlite3.Error and psycopg2.Error are
                    # unrelated types, so catch broadly and keep the spill file.
                    logger.error("Failed to insert drained rows from %s into %s: %s: %s",
                                 spill_path, db_path, type(e).__name__, e)
                    had_error = True
                    last_error = "%s: %s" % (type(e).__name__, e)

            total_drained += file_drained

            # Delete the spill file only if every group landed successfully.
            if not had_error:
                _record_attempt(spill_path, ok=True)
                try:
                    os.remove(spill_path)
                    logger.info("Deleted spill file: %s", spill_path)
                except OSError as e:
                    logger.warning("Failed to delete spill file %s: %s", spill_path, e)
            else:
                # Something in this file refused to drain. Count it; once the
                # budget is spent, move it aside so the loop stops spinning.
                n = _record_attempt(spill_path, ok=False, reason=last_error)
                if n >= _max_attempts:
                    _quarantine_spill(spill_path, last_error or "repeated drain failure", n)
                else:
                    logger.warning(
                        "Spill %s did not fully drain (attempt %d/%d): %s",
                        spill_path, n, _max_attempts, last_error or "unknown")

        except Exception as e:
            logger.error("Error processing spill file %s: %s", spill_path, e)

    return total_drained


async def embed_batch(
    conn: sqlite3.Connection,
    batch: list[tuple[str, str, str, str]],
    dry_run: bool = False,
) -> int:
    """
    Embed a batch of rows. Returns count embedded.
    batch format: [(id, content, title, metadata_json), ...]

    Kept for backwards compatibility. The main() loop now drives
    embed_sweep_lib.run_embed_loop directly; this function remains for
    any external caller that imported it. Internally still uses the
    same memory_core._embed_many primitive — bit-for-bit equivalent
    to the pre-extraction path.
    """
    if not batch:
        return 0

    texts = [content for _, content, _, _ in batch]

    # Import embedding function lazily
    from embedding_utils import pack as _pack
    from memory_core import _embed_many as embed_many

    try:
        embeddings = await embed_many(texts)
    except Exception as e:
        logger.error("Failed to embed batch: %s", e)
        return 0

    if not dry_run:
        try:
            rows_to_insert = []
            for (mem_id, _, _, _), (vec, model_str) in zip(batch, embeddings):
                if vec is not None:
                    packed = _pack(vec)
                    embed_id = str(uuid.uuid4())
                    rows_to_insert.append((
                        embed_id,
                        mem_id,
                        packed,
                        model_str,
                        len(vec),  # dim
                        datetime.now(timezone.utc).isoformat(),
                    ))

            if rows_to_insert:
                conn.executemany(
                    """
                    INSERT OR IGNORE INTO memory_embeddings
                    (id, memory_id, embedding, embed_model, dim, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    rows_to_insert,
                )
                conn.commit()
        except sqlite3.Error as e:
            logger.error("Failed to insert embeddings: %s", e)
            return 0

    return len(batch)


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Check if a table exists in the DB."""
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


async def get_unembed_count(conn: sqlite3.Connection) -> int:
    """Count rows needing embeddings."""
    if not table_exists(conn, "memory_items") or not table_exists(conn, "memory_embeddings"):
        return 0

    try:
        row = conn.execute(
            """
            SELECT COUNT(*) as cnt
            FROM memory_items
            WHERE type='chat_log'
              AND is_deleted=0
              AND id NOT IN (SELECT memory_id FROM memory_embeddings)
            """
        ).fetchone()
        return row["cnt"] if row else 0
    except sqlite3.Error as e:
        logger.error("Failed to count unembedded rows: %s", e)
        return 0


def load_state() -> dict:
    """Load state file, or return defaults."""
    state_path = chatlog_config.STATE_FILE
    if not os.path.exists(state_path):
        return {
            "embed_backlog": 0,
            "last_sweeper_run_at": None,
            "last_sweeper_rows_embedded": 0,
            "last_sweeper_spill_drained": 0,
            "queue_depth": 0,
        }
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Failed to load state file: %s", e)
        return {
            "embed_backlog": 0,
            "last_sweeper_run_at": None,
            "last_sweeper_rows_embedded": 0,
            "last_sweeper_spill_drained": 0,
            "queue_depth": 0,
        }


def save_state(state: dict) -> None:
    """Save state atomically with rename."""
    state_dir = os.path.dirname(chatlog_config.STATE_FILE)
    os.makedirs(state_dir, exist_ok=True)
    tmp_path = chatlog_config.STATE_FILE + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, chatlog_config.STATE_FILE)
    except OSError as e:
        logger.error("Failed to save state: %s", e)


async def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Lazy embed chat log rows missing embeddings."
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=None,
        help="Batch size (default from config.embed_sweeper.batch_size)",
    )
    parser.add_argument(
        "--max-per-run",
        type=int,
        default=None,
        help="Max rows per run (default from CHATLOG_EMBED_MAX_PER_RUN env or 10000)",
    )
    parser.add_argument(
        "--deadline",
        type=float,
        default=None,
        help="Soft wall-clock budget (seconds) for one run. The embed loop stops "
             "starting new batches once this elapses, so a large backlog is drained "
             "across several scheduled runs instead of monopolizing the GPU in one "
             "long run. Default from CHATLOG_EMBED_DEADLINE_S env or 60s; 0 disables "
             "(unbounded, the old behavior). max-per-run still caps total rows.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Query and log but don't embed",
    )
    parser.add_argument(
        "--drain-spill",
        action="store_true",
        help="Process spill files before embedding",
    )
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _task_runtime import add_log_file_arg, setup_task_runtime
    from m3_sdk import add_database_arg
    add_log_file_arg(parser)
    add_database_arg(parser)
    args = parser.parse_args()

    setup_task_runtime(args.log_file, lock_name="chatlog_embed_sweeper")

    if args.database:
        # M3_DATABASE unifies main+chatlog unless CHATLOG_DB_PATH overrides.
        os.environ["M3_DATABASE"] = args.database
        chatlog_config.invalidate_cache()

    # Resolve config
    cfg = chatlog_config.resolve_config()
    batch_size = args.batch or cfg.embed_sweeper.batch_size
    max_per_run = (
        args.max_per_run
        or int(getenv_compat("M3_CHATLOG_EMBED_MAX_PER_RUN", "CHATLOG_EMBED_MAX_PER_RUN", "10000"))
    )
    # Soft time budget per run. None -> env default (60s). 0 -> unbounded (old
    # behavior). Keeps a big backlog from pinning the GPU in one long run; the
    # next scheduled sweep picks up where this one left off (cursor-advanced).
    _deadline = (
        args.deadline
        if args.deadline is not None
        else float(getenv_compat("M3_CHATLOG_EMBED_DEADLINE_S", "CHATLOG_EMBED_DEADLINE_S", "60"))
    )
    # run_embed_loop expects an ABSOLUTE time.monotonic() deadline, not a
    # duration — convert here (a raw duration like 60 would read as "already
    # elapsed" since the monotonic clock is far past 60 at runtime).
    deadline_s = None if _deadline <= 0 else time.monotonic() + _deadline

    # Spill drainage is backend-agnostic and must run BEFORE the SQLite-only
    # setup below (§10a/§3). chatlog_core._spill_batch fires on any flush
    # exception and on queue-full backpressure regardless of backend, and this
    # task is installed regardless of backend — so gating the drain behind a
    # local .db file made it a silent no-op on PostgreSQL, stranding spilled
    # turns on disk with only a log line. drain_spill() routes through the seam.
    early_spill_drained = 0
    if args.drain_spill or os.path.exists(chatlog_config.SPILL_DIR):
        early_spill_drained = await drain_spill()
        if early_spill_drained > 0:
            logger.info("Drained %d rows from spill", early_spill_drained)

    # Open DB connection. The embedding half below is still SQLite-bound (raw
    # sqlite3 cursors + embed_sweep_lib); on a PG deployment the backfill runs
    # via the cognitive loop's embed pass instead, so returning here after the
    # drain is correct, not a silent skip.
    db_path = chatlog_config.chatlog_db_path()
    if not os.path.exists(db_path):
        logger.info(
            "No local chat log DB at %s (expected on a PostgreSQL-backed "
            "deployment); spill drained: %d. Embedding backfill is handled by "
            "the cognitive loop's embed pass.", db_path, early_spill_drained)
        return 0

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        logger.error("Failed to open chat log DB: %s", e)
        return 1

    start_time = asyncio.get_event_loop().time()

    try:
        spill_drained = early_spill_drained
        rows_embedded = 0
        batches_processed = 0

        # Check if schema exists
        if not table_exists(conn, "memory_items"):
            logger.info("Chat log schema not initialized yet; nothing to do")
            return 0

        # Query and embed batches via the shared embed_sweep_lib loop.
        # This delegates concurrency, batching, cursor advance, and per-
        # batch hardening (timeout, oversize/empty/bad-dim skip,
        # consecutive-fail abort) to one place that bin/embed_backfill.py
        # also uses. Behavior change vs. pre-extraction: ORDER BY id ASC
        # instead of ORDER BY created_at ASC. UUIDs aren't time-ordered,
        # so this changes within-batch ordering (negligible at sweeper
        # cadence) and gains infinite-loop protection on skipped rows
        # via the after_id cursor.
        from embed_sweep_lib import Counters as _Counters
        from embed_sweep_lib import run_embed_loop
        from memory_core import (
            _content_hash as _ch,
        )
        from memory_core import (
            _embed_many as _em,
        )
        from memory_core import (
            _pack as _mc_pack,
        )

        counters = _Counters()

        # Fetch callback: pulls (after_id, limit) -> rows. Only chat_log
        # rows still missing an embedding row.
        def _fetch(after_id, limit):
            where = [
                "type='chat_log'",
                "is_deleted=0",
                "id NOT IN (SELECT memory_id FROM memory_embeddings)",
            ]
            params: list = []
            if after_id is not None:
                where.append("id > ?")
                params.append(after_id)
            sql = (
                f"SELECT id, content, title, metadata_json "
                f"FROM memory_items WHERE {' AND '.join(where)} "
                f"ORDER BY id LIMIT ?"
            )
            params.append(limit)
            try:
                return conn.execute(sql, params).fetchall()
            except sqlite3.Error as e:
                logger.error("Failed to query unembedded rows: %s", e)
                return []

        # Write callback: persists one embedding row. Chatlog DB has the
        # content_hash column (added in 003_chroma_sync_queue_align /
        # main migration 021's column add), so we write it.
        def _write(mid: str, vec: list[float], model_str: str, content_hash: str) -> bool:
            if args.dry_run:
                return True  # count it but don't write
            try:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO memory_embeddings
                    (id, memory_id, embedding, embed_model, dim, created_at, content_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        mid,
                        _mc_pack(vec),
                        model_str,
                        len(vec),
                        datetime.now(timezone.utc).isoformat(),
                        content_hash,
                    ),
                )
                conn.commit()
                return cur.rowcount > 0
            except sqlite3.Error as e:
                logger.error("Failed to insert embedding for %s: %s", mid[:8], e)
                return False

        # Drive the loop. limit=max_per_run preserves the existing
        # "max rows per scheduled run" budget.
        await run_embed_loop(
            fetch_candidates=_fetch,
            write_embedding=_write,
            counters=counters,
            embed_many=_em,
            content_hash_fn=_ch,
            transform_text=lambda t, _m: t,  # chatlog: no anchor augmentation
            batch_size=batch_size,
            concurrency=1,                   # sweeper has historically run sequentially
            timeout_s=300.0,                 # generous for a scheduled background job
            deadline_s=deadline_s,           # soft per-run wall-clock budget (see --deadline)
            max_consecutive_fails=5,
            max_row_bytes=32_768,
            # A long chat turn is exactly the row that used to be dropped here:
            # over bge-m3's ctx, skipped every run, invisible to semantic search
            # forever (2026-08-09: 3 such turns, and they also kept the cognitive
            # loop's embed gate permanently "has work"). Subdivide + mean-pool
            # instead, so a long turn is still searchable.
            oversize_mode="subdivide",
            expected_dim=None,               # don't reject by dim — chatlog has heterogenous models
            limit=max_per_run,
            log=lambda msg: logger.info("%s", msg),
        )
        rows_embedded = counters.embedded
        batches_processed = counters.batches_completed

        # Get remaining backlog count
        backlog = await get_unembed_count(conn)

        # Update state
        state = load_state()
        state["embed_backlog"] = backlog
        state["last_sweeper_run_at"] = datetime.now(timezone.utc).isoformat()
        state["last_sweeper_rows_embedded"] = rows_embedded
        state["last_sweeper_spill_drained"] = spill_drained
        save_state(state)

        elapsed = asyncio.get_event_loop().time() - start_time

        if rows_embedded > 0 or spill_drained > 0:
            logger.info(
                "Embedded %d rows in %d batches (%.1fs), spill drained: %d, backlog remaining: %d",
                rows_embedded,
                batches_processed,
                elapsed,
                spill_drained,
                backlog,
            )
        else:
            logger.info("Nothing to do (backlog: %d)", backlog)

        return 0

    except Exception as e:
        logger.exception("Sweeper failed: %s", e)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    # Logging is configured inside main() via setup_task_runtime once args
    # are parsed. A minimal fallback keeps logger output visible if main()
    # raises before that point.
    logging.basicConfig(
        level=logging.INFO,
        format="%(name)s: [%(levelname)s] %(message)s",
    )
    sys.exit(asyncio.run(main()))
