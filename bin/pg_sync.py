from __future__ import annotations

# ============================================================================
# pg_sync.py — Bidirectional SQLite ↔ PostgreSQL sync with per-DB manifests
# ============================================================================
#
# CURRENT SYNC BEHAVIOUR (as of 2026-04-26 refactor, backward-compat preserved)
# ---------------------------------------------------------------------------
# Tables synced from agent_memory.db:
#   1. memory_items           PK: id (UUID)  tombstone: is_deleted (0/1)
#                             Delta via updated_at; full push on first sync.
#                             last-write-wins on updated_at; 'manual'/'system'
#                             change_agent protected (manual wins).
#   2. memory_embeddings      PK: id (UUID)  no own updated_at — delta driven
#                             by parent memory_items.updated_at. FK pre-filter:
#                             embeddings whose memory_item isn't in PG yet are
#                             deferred to next sync (avoids FK violation rollback).
#   3. memory_relationships   PK: id (UUID)  INSERT-only (ON CONFLICT DO NOTHING).
#                             Delta via created_at.
#   4. tasks                  PK: id (UUID)  tombstone: deleted_at (NULL = live,
#                             ISO string = deleted). Delta via updated_at.
#   5. synchronized_secrets   PK: service_name  version+updated_at conflict resolution.
#
# CHANGE DETECTION
#   Watermarks stored in sync_watermarks table (SQLite side, per direction+target).
#   Direction keys: pg_push / pg_pull / rel_push / rel_pull / emb_push / emb_pull /
#                   tasks_push / tasks_pull
#   On first run (no watermark) the full table is synced.
#
# CONFLICT RESOLUTION
#   Last-write-wins on updated_at for most tables. Secrets use version number
#   as primary tiebreaker. memory_embeddings are overwritten on id conflict.
#
# CREDENTIALS
#   PG_URL resolved via: (1) PG_URL env var, (2) m3-memory encrypted vault
#   (ctx.get_secret("PG_URL")).  Falls back to sys.exit(1) with a hint.
#
# MULTI-DB EXTENSION (this refactor)
#   CLI: python bin/pg_sync.py --db <path> [--manifest <path>] [--dry-run]
#   If --manifest omitted, inferred as config/sync_manifests/<db_basename>.yaml.
#   Backward-compat default (no args):
#       --db memory/agent_memory.db
#       --manifest config/sync_manifests/agent_memory.yaml
#   Manifest-driven tables go through _sync_table_generic().
#   Legacy per-table functions (sync_memory_items, sync_memory_embeddings, etc.)
#   are preserved unchanged and called by name for agent_memory.db so that
#   existing test_pg_sync_fk_safety.py continues to pass without modification.
#
# SYNC STATE
#   Watermarks live in sync_watermarks (direction TEXT PK, last_synced_at TEXT).
#   A per-DB, per-table namespace key is constructed as:
#       "<db_stem>_<table>_push" / "<db_stem>_<table>_pull"
#   (for agent_memory.db tables the legacy bare keys are kept to avoid
#    resetting existing watermarks).
# ============================================================================
import argparse
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))
from m3_sdk import resolve_cdw_pg_dsn, resolve_venv_python


def ensure_venv():
    venv_python = resolve_venv_python()
    if os.path.exists(venv_python) and sys.executable != venv_python:
        # venv_python is an absolute path within the project root, so this is safe.
        os.execl(venv_python, venv_python, *sys.argv)  # nosec B606


import json
import logging
import pathlib
import sqlite3
from datetime import datetime, timezone
from typing import Any

import migrate_memory
import yaml
from m3_sdk import M3Context, resolve_db_path

# Python 3.12+ sqlite3 datetime adapter deprecation fix
sqlite3.register_adapter(datetime, lambda val: val.isoformat())

logging.basicConfig(level=logging.INFO, format='%(name)s: [%(levelname)s] %(message)s')
logger = logging.getLogger("pg_sync")

# Initialize SDK context
ctx = M3Context.for_db(None)

BATCH_SIZE = 100  # commit every N rows

# Path to manifest directory relative to repo root
MANIFEST_DIR = os.path.join(BASE_DIR, "config", "sync_manifests")


# ── Manifest loading ─────────────────────────────────────────────────────────

def _load_manifest(manifest_path: str) -> dict[str, Any]:
    """Load and validate a sync manifest YAML file."""
    with open(manifest_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    # Basic validation
    if "tables" not in data:
        raise ValueError(f"Manifest {manifest_path} missing 'tables' key")
    if "sync_order" not in data:
        raise ValueError(f"Manifest {manifest_path} missing 'sync_order' key")

    # Build table lookup
    data["_table_map"] = {t["name"]: t for t in data["tables"]}
    return data


def _infer_manifest_path(db_path: str) -> str:
    """Infer manifest path from db basename: config/sync_manifests/<stem>.yaml"""
    stem = pathlib.Path(db_path).stem  # e.g. "agent_memory"
    return os.path.join(MANIFEST_DIR, f"{stem}.yaml")


# ── Credentials ──────────────────────────────────────────────────────────────

def _get_pg_url() -> str:
    """Resolve the data-warehouse PostgreSQL URL from environment or vault.

    Warehouse role: M3_CDW_PG_URL > PG_URL(deprecated) > vault(PG_URL). Does NOT
    read M3_PG_URL (the primary-store var) — pg_sync fans in to the CDW mirror.
    """
    url = (resolve_cdw_pg_dsn("") or "").strip()
    if url:
        return url
    url = ctx.get_secret("PG_URL")
    if url:
        return url
    logger.error(
        "Data-warehouse DSN not found. Set M3_CDW_PG_URL (or store PG_URL via "
        "`bin/auth_utils.py`). NOTE: M3_PG_URL is the primary-store var, not read here."
    )
    sys.exit(1)


# ── Watermarks ───────────────────────────────────────────────────────────────

def _ensure_watermark_table(sl_cur) -> None:
    """Guarantee the sync_watermarks table exists on the local store.

    Declared by migration 005_perf_and_wal.sql (SQLite) and pg_053 (PostgreSQL),
    so on the MAIN store this is a no-op. It stays because OTHER sync targets —
    agent_chatlog.db above all — run a different migration set that never creates
    it, and there is no migration chain that covers every target. Without the
    self-heal the watermark INSERT raises 'no such table', the delta cursor is
    never persisted, and every sync silently redoes a full reconcile: slow, and
    invisible until someone wonders why sync takes minutes.

    Kept deliberately, unlike sync_all's copy of this DDL (removed in favour of
    pg_053) — that one ran against the PG PRIMARY, which the migration covers.
    Idempotent — safe to call once per target before syncing.
    """
    sl_cur.execute(
        "CREATE TABLE IF NOT EXISTS sync_watermarks "
        "(direction TEXT PRIMARY KEY, last_synced_at TEXT)"
    )


def _get_watermark(sl_cur, direction: str, target_name: str) -> str | None:
    """Read last sync watermark for a direction and target database."""
    # Prefix direction with target_name for separate watermarks per DB
    prefixed_direction = f"{target_name}_{direction}" if target_name != "main" else direction
    try:
        sl_cur.execute(
            f"SELECT last_synced_at FROM sync_watermarks WHERE direction = {_LOCAL_PARAM}",
            (prefixed_direction,))
        row = sl_cur.fetchone()
        return row[0] if row else None
    except Exception as exc:
        # Narrow by MEANING, not by exception class: sqlite3.OperationalError and
        # psycopg2's UndefinedTable are unrelated types for the same condition,
        # and a third backend brings a third. The seam classifies it.
        if not _local_dialect().is_undefined_object_error(exc):
            raise
        # The table is ensured before sync, so this is unexpected — surface it
        # rather than silently forcing a full reconcile every run.
        logger.warning(f"[{target_name}] watermark read failed ({exc}); treating as first sync")
        return None


def _set_watermark(sl_cur, direction: str, ts: str, target_name: str) -> None:
    """Update last sync watermark for a direction and target database."""
    prefixed_direction = f"{target_name}_{direction}" if target_name != "main" else direction
    try:
        sl_cur.execute(
            f"INSERT INTO sync_watermarks (direction, last_synced_at) "
            f"VALUES ({_LOCAL_PARAM}, {_LOCAL_PARAM}) "
            f"{_local_dialect().on_conflict_update('(direction)', ['last_synced_at'])}",
            (prefixed_direction, ts),
        )
    except Exception as exc:
        if not _local_dialect().is_undefined_object_error(exc):
            raise
        # Never swallow silently: an unwritten watermark means the next run
        # re-reconciles the whole table. Ensure the table exists up front.
        logger.error(f"[{target_name}] watermark write failed for {prefixed_direction}: {exc}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _table_exists(sl_cur, table_name: str) -> bool:
    """Check if a table exists in the local store.

    Via the dialect rather than `sqlite_master`: enumerating tables is one of the
    least portable things in SQL, and a hardcoded sqlite_master query returns
    NOTHING on every other engine rather than failing — so every gated table
    would silently be treated as absent and skipped. Silent skip is the worst
    shape for a sync.
    """
    sql, params = _local_dialect().table_exists(table_name)
    sl_cur.execute(sql, params)
    return sl_cur.fetchone() is not None


def _build_conflict_clause(pk_columns: list[str]) -> str:
    """Build ON CONFLICT (col1, col2, ...) clause from pk_columns list."""
    return "(" + ", ".join(pk_columns) + ")"


# ── PG schema helpers (legacy — agent_memory.db only) ────────────────────────

def _ensure_pg_schema(pg_cur):
    """Auto-add columns from newer migrations that PG may be missing."""
    pg_cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'memory_items'
    """)
    existing = {r[0] for r in pg_cur.fetchall()}
    migrations = [
        ("user_id", "ALTER TABLE memory_items ADD COLUMN user_id TEXT DEFAULT ''"),
        ("scope", "ALTER TABLE memory_items ADD COLUMN scope TEXT DEFAULT 'agent'"),
        ("valid_from", "ALTER TABLE memory_items ADD COLUMN valid_from TEXT DEFAULT ''"),
        ("valid_to", "ALTER TABLE memory_items ADD COLUMN valid_to TEXT DEFAULT ''"),
        ("content_hash", "ALTER TABLE memory_items ADD COLUMN content_hash TEXT DEFAULT ''"),
    ]
    added_cols = []
    for col, ddl in migrations:
        if col not in existing:
            pg_cur.execute(ddl)
            added_cols.append(col)
            logger.info(f"PG schema: added missing column '{col}'")

    # Validate that newly added columns actually exist
    if added_cols:
        pg_cur.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'memory_items'
        """)
        post_existing = {r[0] for r in pg_cur.fetchall()}
        for col in added_cols:
            if col not in post_existing:
                raise RuntimeError(f"PG schema migration failed: column '{col}' was not created")


def _ensure_pg_tier_tables(pg_cur):
    """Create agent_retention_policies and gdpr_requests tables in PG if missing."""
    pg_cur.execute("""
        CREATE TABLE IF NOT EXISTS agent_retention_policies (
            agent_id        TEXT PRIMARY KEY,
            max_memories    INTEGER DEFAULT 1000,
            ttl_days        INTEGER DEFAULT 0,
            auto_archive    INTEGER DEFAULT 1,
            created_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at      TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    pg_cur.execute("""
        CREATE TABLE IF NOT EXISTS gdpr_requests (
            id              TEXT PRIMARY KEY,
            subject_id      TEXT NOT NULL,
            request_type    TEXT NOT NULL,
            status          TEXT DEFAULT 'pending',
            items_affected  INTEGER DEFAULT 0,
            requested_at    TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            completed_at    TIMESTAMP WITH TIME ZONE
        )
    """)
    pg_cur.execute("CREATE INDEX IF NOT EXISTS idx_gdpr_subject ON gdpr_requests(subject_id)")
    logger.info("PG schema: ensured agent_retention_policies and gdpr_requests tables exist")


# ── Legacy per-table sync functions (agent_memory.db) ────────────────────────
# These are preserved verbatim so test_pg_sync_fk_safety.py continues to pass.

def sync_memory_items(sl_cur, pg_cur, sl_conn, target_name: str):
    logger.info(f"[{target_name}] Synchronizing memory_items (UUID-based, delta sync)...")
    _ensure_pg_schema(pg_cur)
    now = datetime.now(timezone.utc).isoformat()

    # 1. PUSH: Local to Remote (delta — only changed rows since last push)
    watermark = _get_watermark(sl_cur, "pg_push", target_name)
    if watermark:
        sl_cur.execute(f"""
            SELECT id, type, title, content, metadata_json, agent_id, model_id,
                   change_agent, importance, source, origin_device, is_deleted,
                   expires_at, decay_rate, created_at, updated_at,
                   COALESCE(user_id, '') as user_id, COALESCE(scope, 'agent') as scope,
                   valid_from, valid_to, COALESCE(content_hash, '') as content_hash
            FROM memory_items
            WHERE updated_at > {_LOCAL_PARAM} OR (updated_at IS NULL AND created_at > {_LOCAL_PARAM})
        """, (watermark, watermark))
        logger.info(f"[{target_name}] Delta push: rows changed since {watermark}")
    else:
        sl_cur.execute("""
            SELECT id, type, title, content, metadata_json, agent_id, model_id,
                   change_agent, importance, source, origin_device, is_deleted,
                   expires_at, decay_rate, created_at, updated_at,
                   COALESCE(user_id, '') as user_id, COALESCE(scope, 'agent') as scope,
                   valid_from, valid_to, COALESCE(content_hash, '') as content_hash
            FROM memory_items
        """)
        logger.info(f"[{target_name}] Full push: no watermark found (first sync)")

    local_rows = sl_cur.fetchall()
    push_count = 0
    push_errors = 0

    # SQLite stores timestamps as TEXT with NO validation; PostgreSQL timestamptz
    # is strict. Two failure modes seen in real data that abort an entire batch
    # (and, uncaught, all subsequent batches via the aborted transaction):
    #   - empty-string ''            -> "invalid input syntax for timestamptz"
    #   - out-of-range like 2024-07-37 -> "date/time field value out of range"
    # Coerce any non-parseable timestamp value to None (SQL NULL) on push so one
    # corrupt cell can't block the whole sync. Positions match the SELECT column
    # order above: expires_at(12), created_at(14), updated_at(15), valid_from(18),
    # valid_to(19). A dropped bad timestamp is the least-bad outcome — the row
    # still syncs; the garbage value was never a usable date anyway.
    _TS_POS = (12, 14, 15, 18, 19)

    def _valid_ts(v):
        """True if v parses as a real date/time PG will accept. Cheap: validate
        the leading YYYY-MM-DD[ HH:MM:SS] via datetime; anything else -> False."""
        if v is None:
            return True  # NULL is fine
        if v == "" or not isinstance(v, str):
            return v != ""  # '' invalid; non-str (already a datetime) assume ok
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
            return True
        except ValueError:
            # Fall back to a lenient date-only parse for 'YYYY-MM-DD ...' forms;
            # fromisoformat already covers most, so a failure here means the
            # value is genuinely malformed (bad month/day, junk) -> NULL it.
            return False

    def _norm_ts(row):
        r = list(row)
        for p in _TS_POS:
            if p < len(r) and not _valid_ts(r[p]):
                r[p] = None
        return tuple(r)
    local_rows = [_norm_ts(r) for r in local_rows]

    # Batch UPSERT using execute_values or manual batching for PostgreSQL
    from psycopg2.extras import execute_values

    if local_rows:
        try:
            # PostgreSQL upsert logic for memory_items
            upsert_query = """
                INSERT INTO memory_items (
                    id, type, title, content, metadata_json, agent_id, model_id,
                    change_agent, importance, source, origin_device, is_deleted,
                    expires_at, decay_rate, created_at, updated_at, user_id, scope,
                    valid_from, valid_to, content_hash
                ) VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    content = EXCLUDED.content,
                    metadata_json = EXCLUDED.metadata_json,
                    updated_at = EXCLUDED.updated_at,
                    is_deleted = EXCLUDED.is_deleted,
                    change_agent = EXCLUDED.change_agent,
                    user_id = EXCLUDED.user_id,
                    scope = EXCLUDED.scope,
                    valid_from = EXCLUDED.valid_from,
                    valid_to = EXCLUDED.valid_to,
                    content_hash = EXCLUDED.content_hash
                WHERE (memory_items.updated_at IS NULL OR EXCLUDED.updated_at > memory_items.updated_at)
                  AND (
                      (memory_items.change_agent NOT IN ('manual', 'system'))
                      OR (EXCLUDED.change_agent = 'manual')
                  )
            """
            # Process in batches
            for i in range(0, len(local_rows), BATCH_SIZE):
                batch = local_rows[i:i+BATCH_SIZE]
                execute_values(pg_cur, upsert_query, batch)
                push_count += len(batch)
        except Exception as exc:
            logger.error(f"[{target_name}] Batch push failed: {type(exc).__name__}: {exc}")
            push_errors = len(local_rows)

    _set_watermark(sl_cur, "pg_push", now, target_name)
    sl_conn.commit()
    logger.info(f"[{target_name}] Pushed {push_count} local memory items ({push_errors} errors).")

    # 2. PULL: Remote to Local (delta — only changed rows since last pull)
    watermark = _get_watermark(sl_cur, "pg_pull", target_name)
    # Watermark from the PG server clock (captured before the pull), not local
    # `now`: the delta compares against PG-side updated_at from other peers'
    # clocks, so a skewed local clock can silently skip rows.
    try:
        pg_cur.execute("SELECT NOW()")
        _pn = pg_cur.fetchone()
        pull_watermark = _pn[0].isoformat() if _pn else now
    except Exception:
        pull_watermark = now
    if watermark:
        pg_cur.execute("""
            SELECT id, type, title, content, metadata_json, agent_id, model_id,
                   change_agent, importance, source, origin_device, is_deleted,
                   expires_at, decay_rate, created_at, updated_at,
                   COALESCE(user_id, '') as user_id, COALESCE(scope, 'agent') as scope,
                   valid_from, valid_to, COALESCE(content_hash, '') as content_hash
            FROM memory_items
            WHERE updated_at > %s OR (updated_at IS NULL AND created_at > %s)
        """, (watermark, watermark))
        logger.info(f"[{target_name}] Delta pull: rows changed since {watermark}")
    else:
        pg_cur.execute("""
            SELECT id, type, title, content, metadata_json, agent_id, model_id,
                   change_agent, importance, source, origin_device, is_deleted,
                   expires_at, decay_rate, created_at, updated_at,
                   COALESCE(user_id, '') as user_id, COALESCE(scope, 'agent') as scope,
                   valid_from, valid_to, COALESCE(content_hash, '') as content_hash
            FROM memory_items
        """)
        logger.info(f"[{target_name}] Full pull: no watermark found (first sync)")

    remote_rows = pg_cur.fetchall()
    pull_count = 0
    pull_errors = 0

    if remote_rows:
        try:
            cols = [
                "id", "type", "title", "content", "metadata_json", "agent_id",
                "model_id", "change_agent", "importance", "source", "origin_device",
                "is_deleted", "expires_at", "decay_rate", "created_at", "updated_at",
                "user_id", "scope", "valid_from", "valid_to", "content_hash",
            ]
            # Columns the remote may overwrite. Deliberately NOT every column:
            # created_at, agent_id, importance and friends stay as first written.
            update_cols = [
                "title", "content", "metadata_json", "updated_at", "is_deleted",
                "change_agent", "user_id", "scope", "valid_from", "valid_to",
                "content_hash",
            ]
            # Last-write-wins, plus the manual/system protection: a locally
            # curated row is not overwritten by an automated remote one, but a
            # remote MANUAL edit still wins. Passed through verbatim — the text is
            # valid on both backends (excluded/EXCLUDED are interchangeable).
            guard = (
                "WHERE (memory_items.updated_at IS NULL "
                "OR excluded.updated_at > memory_items.updated_at) "
                "AND ((memory_items.change_agent NOT IN ('manual', 'system')) "
                "OR (excluded.change_agent = 'manual'))"
            )
            for i in range(0, len(remote_rows), BATCH_SIZE):
                batch = []
                for row in remote_rows[i:i+BATCH_SIZE]:
                    row_list = list(row)
                    if isinstance(row_list[4], dict):
                        row_list[4] = json.dumps(row_list[4])
                    batch.append(tuple(row_list))

                pull_count += _local_backend().bulk_upsert(
                    sl_conn, "memory_items", cols, batch,
                    conflict_target="(id)", update_columns=update_cols,
                    guard_sql=guard,
                )
                sl_conn.commit()
        except Exception as exc:
            logger.error(f"[{target_name}] Batch pull failed: {type(exc).__name__}: {exc}")
            pull_errors = len(remote_rows)

    _set_watermark(sl_cur, "pg_pull", pull_watermark, target_name)
    logger.info(f"[{target_name}] Pulled {pull_count} remote memory items ({pull_errors} errors).")


def sync_memory_relationships(sl_cur, pg_cur, sl_conn, target_name: str):
    """Synchronizes the memory_relationships table bi-directionally with watermark logic."""
    logger.info(f"[{target_name}] Synchronizing memory_relationships...")
    from psycopg2.extras import execute_values
    now = datetime.now(timezone.utc).isoformat()

    # 1. PUSH: Local to Remote
    watermark = _get_watermark(sl_cur, "rel_push", target_name)
    if watermark:
        sl_cur.execute(
            f"SELECT id, from_id, to_id, relationship_type, created_at "
            f"FROM memory_relationships WHERE created_at > {_LOCAL_PARAM}",
            (watermark,))
    else:
        sl_cur.execute("SELECT id, from_id, to_id, relationship_type, created_at FROM memory_relationships")

    local_rows = sl_cur.fetchall()
    push_count = 0
    if local_rows:
        try:
            execute_values(pg_cur, "INSERT INTO memory_relationships (id, from_id, to_id, relationship_type, created_at) VALUES %s ON CONFLICT (id) DO NOTHING", local_rows)
            push_count = len(local_rows)
        except Exception as exc:
            logger.warning(f"[{target_name}] Batch Relationship push failed: {type(exc).__name__}")

    _set_watermark(sl_cur, "rel_push", now, target_name)
    sl_conn.commit()
    logger.info(f"[{target_name}] Pushed {push_count} relationships to warehouse.")

    # 2. PULL: Remote to Local
    watermark = _get_watermark(sl_cur, "rel_pull", target_name)
    # PG-server-clock watermark (see sync_memory_items) — avoids clock-skew drops.
    try:
        pg_cur.execute("SELECT NOW()")
        _pn = pg_cur.fetchone()
        pull_watermark = _pn[0].isoformat() if _pn else now
    except Exception:
        pull_watermark = now
    if watermark:
        pg_cur.execute("SELECT id, from_id, to_id, relationship_type, created_at FROM memory_relationships WHERE created_at > %s", (watermark,))
    else:
        pg_cur.execute("SELECT id, from_id, to_id, relationship_type, created_at FROM memory_relationships")

    remote_rows = pg_cur.fetchall()
    pull_count = 0
    if remote_rows:
        try:
            # DO NOTHING, not an upsert: an edge is immutable, so an existing row
            # is authoritative and a re-push must be a no-op. The two backends
            # spell that at OPPOSITE ENDS of the statement (SQLite in the verb,
            # PG in a trailing clause), which is why it is a seam primitive
            # rather than a template a caller could fill in.
            pull_count = _local_backend().bulk_insert_ignore(
                sl_conn, "memory_relationships",
                ["id", "from_id", "to_id", "relationship_type", "created_at"],
                [tuple(r) for r in remote_rows],
                conflict_target="(id)",
            )
            sl_conn.commit()
        except Exception as exc:
            logger.warning(f"[{target_name}] Batch Relationship pull failed: {type(exc).__name__}")

    _set_watermark(sl_cur, "rel_pull", pull_watermark, target_name)
    sl_conn.commit()
    logger.info(f"[{target_name}] Pulled {pull_count} relationships from warehouse.")


def sync_secrets(sl_cur, pg_cur, target_name: str):
    logger.info(f"[{target_name}] Synchronizing encrypted secrets vault...")
    from psycopg2.extras import execute_values

    # 1. PUSH: Local to Remote
    sl_cur.execute("SELECT service_name, encrypted_value, version, origin_device, updated_at FROM synchronized_secrets")
    local_rows = sl_cur.fetchall()
    push_count = 0
    if local_rows:
        try:
            execute_values(pg_cur, """
                INSERT INTO synchronized_secrets (service_name, encrypted_value, version, origin_device, updated_at)
                VALUES %s
                ON CONFLICT (service_name) DO UPDATE SET
                    encrypted_value = EXCLUDED.encrypted_value,
                    version = EXCLUDED.version,
                    origin_device = EXCLUDED.origin_device,
                    updated_at = EXCLUDED.updated_at
                WHERE EXCLUDED.version > synchronized_secrets.version
                   OR (EXCLUDED.version = synchronized_secrets.version AND EXCLUDED.updated_at > synchronized_secrets.updated_at)
            """, local_rows)
            push_count = len(local_rows)
        except Exception as exc:
            logger.warning(f"[{target_name}] Batch Secret push failed: {type(exc).__name__}")

    logger.info(f"[{target_name}] Pushed {push_count} local secrets to the warehouse.")

    # 2. PULL: Remote to Local
    pg_cur.execute("SELECT service_name, encrypted_value, version, origin_device, updated_at FROM synchronized_secrets")
    remote_rows = pg_cur.fetchall()
    pull_count = 0
    if remote_rows:
        try:
            # ⚠ VERSION precedence, NOT last-write-wins. A secret's version number
            # is authoritative; the timestamp only breaks ties WITHIN one version.
            # Reshaping this into an updated_at comparison would silently
            # mis-merge the highest-value rows in the store — which is exactly
            # why bulk_upsert takes guard_sql as a pass-through fragment rather
            # than an LWW-shaped parameter.
            guard = (
                "WHERE excluded.version > synchronized_secrets.version "
                "OR (excluded.version = synchronized_secrets.version "
                "AND excluded.updated_at > synchronized_secrets.updated_at)"
            )
            pull_count = _local_backend().bulk_upsert(
                sl_cur.connection, "synchronized_secrets",
                ["service_name", "encrypted_value", "version", "origin_device",
                 "updated_at"],
                [tuple(r) for r in remote_rows],
                conflict_target="(service_name)",
                update_columns=["encrypted_value", "version", "origin_device",
                                "updated_at"],
                guard_sql=guard,
            )
        except Exception as exc:
            logger.warning(f"[{target_name}] Batch Secret pull failed: {type(exc).__name__}")

    logger.info(f"[{target_name}] Pulled {pull_count} remote secrets from the warehouse.")


def _ensure_pg_tasks_schema(pg_cur):
    """Create the `tasks` table in PG if missing, with tombstone column."""
    pg_cur.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id                TEXT PRIMARY KEY,
            title             TEXT NOT NULL,
            description       TEXT DEFAULT '',
            state             TEXT NOT NULL DEFAULT 'pending',
            owner_agent       TEXT,
            created_by        TEXT NOT NULL,
            parent_task_id    TEXT,
            result_memory_id  TEXT,
            metadata_json     TEXT DEFAULT '{}',
            created_at        TEXT,
            updated_at        TEXT,
            completed_at      TEXT,
            deleted_at        TEXT
        )
    """)
    pg_cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS deleted_at TEXT")
    pg_cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_owner_state ON tasks(owner_agent, state)")
    pg_cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_parent      ON tasks(parent_task_id)")
    pg_cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_state       ON tasks(state)")
    pg_cur.execute("CREATE INDEX IF NOT EXISTS idx_tasks_deleted_at  ON tasks(deleted_at)")
    logger.info("PG schema: ensured tasks table exists (with deleted_at tombstone column)")


def sync_tasks(sl_cur, pg_cur, sl_conn, target_name: str):
    """Bi-directional delta sync for the tasks table, including soft-delete tombstones.

    Tombstones ride through as ordinary UPSERTs: deleted_at is just a column,
    and any change to it bumps updated_at, so delta watermarks pick it up for
    free. Sync is UPSERT-only (no DELETE propagation) — cross-peer hard-delete
    is out of scope; peers converge via tombstones.
    """
    logger.info(f"[{target_name}] Synchronizing tasks...")
    from psycopg2.extras import execute_values
    now = datetime.now(timezone.utc).isoformat()

    _ensure_pg_tasks_schema(pg_cur)

    task_cols = (
        "id, title, description, state, owner_agent, created_by, parent_task_id, "
        "result_memory_id, metadata_json, created_at, updated_at, completed_at, deleted_at"
    )

    # 1. PUSH: local → remote (delta on updated_at)
    watermark = _get_watermark(sl_cur, "tasks_push", target_name)
    if watermark:
        sl_cur.execute(
            f"SELECT {task_cols} FROM tasks WHERE updated_at > {_LOCAL_PARAM} "
            f"OR (updated_at IS NULL AND created_at > {_LOCAL_PARAM})",
            (watermark, watermark),
        )
        logger.info(f"[{target_name}] Delta task push: rows changed since {watermark}")
    else:
        sl_cur.execute(f"SELECT {task_cols} FROM tasks")
        logger.info(f"[{target_name}] Full task push: no watermark found (first sync)")

    local_rows = sl_cur.fetchall()
    push_count = 0
    if local_rows:
        try:
            upsert = f"""
                INSERT INTO tasks ({task_cols}) VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    title            = EXCLUDED.title,
                    description      = EXCLUDED.description,
                    state            = EXCLUDED.state,
                    owner_agent      = EXCLUDED.owner_agent,
                    parent_task_id   = EXCLUDED.parent_task_id,
                    result_memory_id = EXCLUDED.result_memory_id,
                    metadata_json    = EXCLUDED.metadata_json,
                    updated_at       = EXCLUDED.updated_at,
                    completed_at     = EXCLUDED.completed_at,
                    deleted_at       = EXCLUDED.deleted_at
                WHERE tasks.updated_at IS NULL OR EXCLUDED.updated_at > tasks.updated_at
            """
            for i in range(0, len(local_rows), BATCH_SIZE):
                batch = [tuple(r) for r in local_rows[i:i+BATCH_SIZE]]
                execute_values(pg_cur, upsert, batch)
                push_count += len(batch)
        except Exception as exc:
            logger.error(f"[{target_name}] Batch task push failed: {type(exc).__name__}: {exc}")

    _set_watermark(sl_cur, "tasks_push", now, target_name)
    sl_conn.commit()
    logger.info(f"[{target_name}] Pushed {push_count} tasks to warehouse.")

    # 2. PULL: remote → local (delta on updated_at)
    watermark = _get_watermark(sl_cur, "tasks_pull", target_name)
    if watermark:
        pg_cur.execute(
            f"SELECT {task_cols} FROM tasks WHERE updated_at > %s OR (updated_at IS NULL AND created_at > %s)",
            (watermark, watermark),
        )
        logger.info(f"[{target_name}] Delta task pull: rows changed since {watermark}")
    else:
        pg_cur.execute(f"SELECT {task_cols} FROM tasks")
        logger.info(f"[{target_name}] Full task pull: no watermark found (first sync)")

    remote_rows = pg_cur.fetchall()
    pull_count = 0
    if remote_rows:
        try:
            # Columns the remote may overwrite. Deliberately NOT `created_by` or
            # `created_at`: provenance stays as first written.
            update_cols = [
                "title", "description", "state", "owner_agent", "parent_task_id",
                "result_memory_id", "metadata_json", "updated_at", "completed_at",
                "deleted_at",
            ]
            guard = ("WHERE tasks.updated_at IS NULL "
                     "OR excluded.updated_at > tasks.updated_at")
            cols = [c.strip() for c in task_cols.split(",")]
            for i in range(0, len(remote_rows), BATCH_SIZE):
                batch = [tuple(r) for r in remote_rows[i:i+BATCH_SIZE]]
                pull_count += _local_backend().bulk_upsert(
                    sl_conn, "tasks", cols, batch,
                    conflict_target="(id)", update_columns=update_cols,
                    guard_sql=guard,
                )
                sl_conn.commit()
        except Exception as exc:
            logger.error(f"[{target_name}] Batch task pull failed: {type(exc).__name__}: {exc}")

    _set_watermark(sl_cur, "tasks_pull", now, target_name)
    sl_conn.commit()
    logger.info(f"[{target_name}] Pulled {pull_count} tasks from warehouse.")


def _ensure_pg_embeddings_schema(pg_cur):
    """Auto-create memory_embeddings table in PG if missing."""
    pg_cur.execute("""
        SELECT EXISTS (
            SELECT FROM information_schema.tables
            WHERE table_name = 'memory_embeddings'
        )
    """)
    if not pg_cur.fetchone()[0]:
        pg_cur.execute("""
            CREATE TABLE memory_embeddings (
                id TEXT PRIMARY KEY,
                memory_id TEXT NOT NULL,
                embedding BYTEA NOT NULL,
                embed_model TEXT DEFAULT 'qwen3-embedding',
                dim INTEGER DEFAULT 1024,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            )
        """)
        pg_cur.execute("CREATE INDEX idx_me_memory_id ON memory_embeddings(memory_id)")
        logger.info("PG schema: created memory_embeddings table")


def _embed_families(cur) -> set:
    """Distinct embedding model FAMILIES present in a store (via its cursor).
    Two tags of ONE model (the in-process GGUF vs the LM Studio server tag) map
    to the same family, so they never look like a mismatch."""
    try:
        from doctor.embed_space_probe import _family
    except Exception:  # noqa: BLE001 — fall back to a crude normaliser
        def _family(t):  # type: ignore[misc]
            return (t or "").strip().lower()
    try:
        cur.execute("SELECT DISTINCT embed_model FROM memory_embeddings")
        return {_family(str(t or "")) for (t,) in cur.fetchall() if t}
    except Exception:  # noqa: BLE001 — never let a diagnostic abort the sync
        return set()


def _warn_on_embed_family_mismatch(sl_cur, pg_cur, target_name: str) -> None:
    """Warn LOUDLY when local and warehouse stores embed with DIFFERENT model
    families. Mixing families in one vector space makes cosine ranking
    meaningless (bge-m3 vs qwen3 vs nomic are not comparable), which is exactly
    how CDW pulls silently degraded search. Warn-only: the sync still proceeds
    (refusing would strand a fleet mid-migration), but the operator is told to
    converge on one family and re-embed. `m3 doctor`'s embed-space probe reports
    the resulting mix per store."""
    local = _embed_families(sl_cur)
    remote = _embed_families(pg_cur)
    if not local or not remote:
        return
    if len(local | remote) > 1:
        logger.warning(
            f"[{target_name}] EMBED-FAMILY MISMATCH: local={sorted(local)} "
            f"warehouse={sorted(remote)} — syncing MIXES incomparable vector "
            "spaces and degrades search ranking. Converge every node on ONE "
            "embed family, then re-embed (bin/reembed_space.py). See `m3 doctor` "
            "embed-space check."
        )


def sync_memory_embeddings(sl_cur, pg_cur, sl_conn, target_name: str):
    """Synchronizes the memory_embeddings table bi-directionally with watermark logic."""
    logger.info(f"[{target_name}] Synchronizing memory_embeddings...")
    from psycopg2 import Binary
    from psycopg2.extras import execute_values
    now = datetime.now(timezone.utc).isoformat()

    _ensure_pg_embeddings_schema(pg_cur)
    # Fleet guardrail: a warehouse embedded with a different model family than
    # this node silently poisons the shared vector space on sync (see docstring).
    _warn_on_embed_family_mismatch(sl_cur, pg_cur, target_name)

    # 1. PUSH: Local to Remote (delta)
    watermark = _get_watermark(sl_cur, "emb_push", target_name)
    if watermark:
        # memory_embeddings has no updated_at, so filter by parent memory_item timestamps
        sl_cur.execute(f"""
            SELECT id, memory_id, embedding, embed_model, dim
            FROM memory_embeddings
            WHERE memory_id IN (
                SELECT id FROM memory_items
                WHERE updated_at > {_LOCAL_PARAM} OR (updated_at IS NULL AND created_at > {_LOCAL_PARAM})
            )
        """, (watermark, watermark))
        logger.info(f"[{target_name}] Delta embedding push: rows changed since {watermark}")
    else:
        sl_cur.execute("SELECT id, memory_id, embedding, embed_model, dim FROM memory_embeddings")
        logger.info(f"[{target_name}] Full embedding push: no watermark found (first sync)")

    local_rows = sl_cur.fetchall()
    push_count = 0
    push_errors = 0
    skipped_fk = 0

    if local_rows:
        # Pre-filter: drop embeddings whose parent memory_item hasn't landed in
        # PG yet. Otherwise the FK fires and rolls back the whole batch. These
        # embeddings re-queue on the next sync after their parent lands.
        candidate_ids = [row[1] for row in local_rows]
        existing_ids: set[str] = set()
        for i in range(0, len(candidate_ids), BATCH_SIZE):
            chunk = candidate_ids[i:i+BATCH_SIZE]
            pg_cur.execute(
                "SELECT mi.id FROM memory_items mi WHERE mi.id = ANY(%s)", (chunk,)
            )
            existing_ids.update(r[0] for r in pg_cur.fetchall())

        filtered_rows = [r for r in local_rows if r[1] in existing_ids]
        skipped_fk = len(local_rows) - len(filtered_rows)
        if skipped_fk:
            logger.info(
                f"[{target_name}] Skipping {skipped_fk} embeddings whose memory_item is not yet in PG"
            )

        try:
            # Convert sqlite3.Row to tuples with Binary-wrapped embedding blobs
            values = []
            for row in filtered_rows:
                row_list = list(row)
                # row[2] is the embedding blob — wrap for PG BYTEA
                row_list[2] = Binary(row_list[2])
                values.append(tuple(row_list))

            for i in range(0, len(values), BATCH_SIZE):
                batch = values[i:i+BATCH_SIZE]
                execute_values(pg_cur, """
                    INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim)
                    VALUES %s
                    ON CONFLICT (id) DO UPDATE SET
                        embedding = EXCLUDED.embedding,
                        embed_model = EXCLUDED.embed_model,
                        dim = EXCLUDED.dim
                """, batch)
                push_count += len(batch)
        except Exception as exc:
            logger.error(f"[{target_name}] Batch embedding push failed: {type(exc).__name__}: {exc}")
            push_errors = len(filtered_rows)

    _set_watermark(sl_cur, "emb_push", now, target_name)
    sl_conn.commit()
    logger.info(
        f"[{target_name}] Pushed {push_count} embeddings to warehouse "
        f"({push_errors} errors, {skipped_fk} deferred for missing parent)."
    )

    # 2. PULL: Remote to Local (delta)
    watermark = _get_watermark(sl_cur, "emb_pull", target_name)
    if watermark:
        pg_cur.execute("""
            SELECT me.id, me.memory_id, me.embedding, me.embed_model, me.dim
            FROM memory_embeddings me
            JOIN memory_items mi ON me.memory_id = mi.id
            WHERE mi.updated_at > %s OR (mi.updated_at IS NULL AND mi.created_at > %s)
        """, (watermark, watermark))
        logger.info(f"[{target_name}] Delta embedding pull: rows changed since {watermark}")
    else:
        pg_cur.execute("SELECT id, memory_id, embedding, embed_model, dim FROM memory_embeddings")
        logger.info(f"[{target_name}] Full embedding pull: no watermark found (first sync)")

    remote_rows = pg_cur.fetchall()
    pull_count = 0
    pull_errors = 0

    if remote_rows:
        try:
            for i in range(0, len(remote_rows), BATCH_SIZE):
                batch = []
                for row in remote_rows[i:i+BATCH_SIZE]:
                    row_list = list(row)
                    # PG returns memoryview for BYTEA — convert to bytes
                    if isinstance(row_list[2], memoryview):
                        row_list[2] = bytes(row_list[2])
                    batch.append(tuple(row_list))

                # No guard, deliberately: an embedding is DERIVED from its
                # memory's content, so the newest vector for a given id is always
                # the right one and there is no edit to protect. The parent
                # memory_items merge carries the last-write-wins decision; adding
                # a second one here could strand a row's vector a version behind
                # its content.
                pull_count += _local_backend().bulk_upsert(
                    sl_conn, "memory_embeddings",
                    ["id", "memory_id", "embedding", "embed_model", "dim"],
                    [tuple(r) for r in batch],
                    conflict_target="(id)",
                    update_columns=["embedding", "embed_model", "dim"],
                )
                sl_conn.commit()
        except Exception as exc:
            logger.error(f"[{target_name}] Batch embedding pull failed: {type(exc).__name__}: {exc}")
            pull_errors = len(remote_rows)

    _set_watermark(sl_cur, "emb_pull", now, target_name)
    sl_conn.commit()
    logger.info(f"[{target_name}] Pulled {pull_count} embeddings from warehouse ({pull_errors} errors).")


# ── Sync lock ─────────────────────────────────────────────────────────────────

def _set_warehouse_search_path(pg_conn) -> str:
    """If the target PG holds the warehouse schema `m3_warehouse`, prepend it to
    the connection's search_path so the sync's UNQUALIFIED table names
    (memory_items, memory_embeddings, ...) resolve there instead of `public`.

    The CDW warehouse stores memory under `m3_warehouse` (pg_warehouse_chatlog_v1.sql:
    unified core+chat, type='chat_log' index), but pg_sync was written for the old
    public-schema primary layout and writes unqualified names — so without this the
    memory/embedding sync hits `public` and fails "relation memory_items does not
    exist" (2026-07-19 root cause) while tasks/secrets (still in public) succeed.

    Probes to_regclass so it's a no-op on a public-only/primary target (search_path
    unchanged). Returns the schema in effect ('m3_warehouse' or 'public'). Never
    raises — a probe failure leaves the default search_path (fail-safe to old
    behavior)."""
    try:
        with pg_conn.cursor() as cur:
            # Visible to this role? to_regclass returns NULL if the table is absent
            # OR the role lacks schema USAGE — either way we must not switch. A
            # permission-denied here raises and would poison the transaction for
            # the sync work that follows, so roll back on ANY failure to leave a
            # clean transaction on the default (public) search_path.
            cur.execute("SELECT to_regclass('m3_warehouse.memory_items')")
            if cur.fetchone()[0] is not None:
                cur.execute("SET search_path TO m3_warehouse, public")
                logger.info("PG search_path set to m3_warehouse (warehouse layout detected).")
                return "m3_warehouse"
    except Exception as e:
        logger.warning(f"Could not probe/set warehouse search_path ({e}); using default.")
        try:
            pg_conn.rollback()  # clear the aborted-transaction state
        except Exception:
            pass
    return "public"


def _ensure_sync_lock_table(sl_cur) -> None:
    """Guarantee the sync_locks table exists.

    Declared by migration 044 (SQLite) / pg_053 (PostgreSQL), so on a
    migrated store this is a no-op. It stays as a self-heal for the case that
    caused the 2026-07-19 outage: the lock used to live in `sync_state`, a
    ChromaDB table that migration 040 legitimately dropped, after which the lock
    SELECT raised "no such table", a bare-except read that as "another sync is in
    progress", and EVERY sync silently skipped thereafter.

    A store that predates 044, or a target DB running a different migration set,
    must therefore still be able to take the lock on first run rather than
    wedging. Idempotent."""
    sl_cur.execute(
        "CREATE TABLE IF NOT EXISTS sync_locks "
        "(lock_name TEXT PRIMARY KEY, holder TEXT, acquired_at TEXT)"
    )


# Staleness ceiling for a lock whose owner PID we can't evaluate (e.g. a legacy
# row with no PID, or a PID from a since-recycled process on another host). PID
# liveness handles the common same-host crash immediately; this bounds the rest.
_SYNC_LOCK_STALE_SECONDS = 3600

# The single row in sync_locks that pg_sync coordinates on. Named rather than a
# literal so the acquire/release/probe sites cannot drift apart.
_SYNC_LOCK_NAME = "pg_sync"

# Bind placeholder for the LOCAL store. The local half is SQLite today (Phase 3
# ports it), but a literal '?' in feature code is a portability bug the moment a
# second local backend lands — ask the seam, which knows. Falls back to the
# SQLite form when the seam is unavailable (installer bootstrap), matching
# auth_utils._dialect_param's own fallback.
def _local_dialect():
    """The LOCAL store's dialect — placeholders, upsert clause, error classes.

    Resolved per call rather than cached: unlike _LOCAL_PARAM (a string baked at
    import for use inside f-strings), the dialect object is consulted for
    behaviour, and a test that flips M3_DB_BACKEND must see the change.

    Falls back to the SQLite dialect when the seam is unavailable (installer
    bootstrap), matching auth_utils._dialect_param's own fallback — the local
    store IS SQLite in every deployment that can reach that state.
    """
    try:
        from memory.backends import dialect
        return dialect()
    except Exception:  # noqa: BLE001 — bootstrap: seam not importable yet
        from memory.backends.dialect import dialect_for
        return dialect_for("sqlite")


def _local_backend():
    """The LOCAL store's backend, for seam primitives like bulk_upsert.

    Returns the ACTIVE backend: the local store is whatever this deployment is
    configured to use. Sync's remote half is addressed separately (an explicit
    warehouse DSN), so there is no ambiguity about which store this means.

    Cheap — active_backend() is memoized — so call sites need not thread it
    through as a parameter, which keeps the sync_* signatures unchanged.
    """
    from memory.backends import active_backend

    return active_backend()


def _local_param() -> str:
    try:
        return _local_dialect().param()
    except Exception:  # noqa: BLE001 — bootstrap: seam not importable yet
        return "?"


_LOCAL_PARAM = _local_param()


def _this_host() -> str:
    """This machine's name, for lock ownership. Never raises."""
    try:
        import socket
        return socket.gethostname() or "unknown-host"
    except Exception:  # noqa: BLE001 — a lock must not fail to be taken over this
        return "unknown-host"


def _lock_value(now_iso: str) -> str:
    """The value stored in the lock row: '<iso_timestamp>|<pid>@<host>'.

    The PID lets a crashed sync's lock be reclaimed IMMEDIATELY (the process is
    gone) instead of waiting out the staleness window.

    The HOST matters once the store can be shared. On SQLite the lock lives in a
    per-machine file, so a recorded PID is always local and always meaningful.
    On a PostgreSQL primary that two machines both sync from, they share one lock
    row — and a bare PID lets machine A read machine B's live PID, find no such
    process locally, judge the lock stale and steal it: two concurrent syncs.
    Recording the host lets a reader tell "not my host, cannot evaluate" from
    "my host, the process is gone".

    Backward-compatible in both directions: a legacy '<ts>|<pid>' value parses
    with host=None, and a legacy READER seeing '<pid>@<host>' fails its int()
    and falls back to the timestamp ceiling — the safe direction.
    """
    return f"{now_iso}|{os.getpid()}@{_this_host()}"


def _parse_lock_value(raw: str) -> "tuple[str, int | None, str | None]":
    """Split a lock value into (iso_timestamp, pid|None, host|None).

    Tolerates the legacy '<ts>|<pid>' and '<ts>' forms, both of which yield
    host=None and are then treated as unevaluable ownership.
    """
    ts, _, rest = raw.partition("|")
    if not rest:
        return ts, None, None
    pid_s, _, host = rest.partition("@")
    try:
        return ts, (int(pid_s) if pid_s else None), (host or None)
    except ValueError:
        return ts, None, (host or None)


def _sync_lock_is_stale(raw: str) -> bool:
    """True if a held lock can be stolen.

    A PID is only evidence on the host that wrote it, so the PID shortcut applies
    only when the lock names THIS host. Anything else — a foreign host, a legacy
    value with no host, an unparseable PID — falls through to the staleness
    ceiling, which is the conservative direction: waiting out the window costs a
    delayed sync, whereas stealing a live lock costs two concurrent writers.
    """
    ts, pid, host = _parse_lock_value(raw)
    # A legacy value carries no host. Treating it as foreign would COST the
    # immediate crash-recovery this check exists for — a dead-pid lock left by
    # the previous build would block for the full hour. Legacy values only ever
    # came from a per-machine SQLite file, where the pid WAS local by
    # construction, so treating them as same-host preserves the old behaviour
    # exactly. A shared store can only contain host-tagged values, since only
    # this build writes there.
    same_host = host is None or host == _this_host()
    if pid is not None and same_host:
        # Same-host crash recovery: the recorded process is gone -> abandoned.
        # (A live PID means a sync really is in progress -> not stale.)
        try:
            from m3_halt import pid_is_alive
            if not pid_is_alive(pid):
                return True
        except Exception:
            pass  # fall through to the timestamp check
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds()
        return age >= _SYNC_LOCK_STALE_SECONDS
    except ValueError:
        return True  # unparseable timestamp -> treat as stale, don't wedge forever


def _acquire_sync_lock(sl_cur) -> bool:
    """Attempts to acquire a global sync lock. Returns True if successful.

    A MISSING lock table is "not locked" (create + acquire), NOT "locked" — the
    latter was a footgun that silently skipped every sync on any DB whose lock
    table was absent (see _ensure_sync_lock_table for the outage). A HELD lock
    whose owner process has died is reclaimed immediately (PID liveness), but
    ONLY when the lock names this host — see _sync_lock_is_stale."""
    # Self-heal: a missing table must never read as "held". Create-if-absent
    # BEFORE the lock check so the SELECT can't fail with 'no such table'.
    try:
        _ensure_sync_lock_table(sl_cur)
    except Exception as e:
        # If we can't even create the table, we cannot coordinate — proceed
        # UNLOCKED (fail-open) rather than block all syncs forever. A rare
        # concurrent double-sync is far less harmful than a permanent no-sync.
        logger.warning(f"Could not ensure sync_locks table ({e}); proceeding without lock.")
        return True
    try:
        sl_cur.execute(
            f"SELECT holder FROM sync_locks WHERE lock_name = {_LOCAL_PARAM}",
            (_SYNC_LOCK_NAME,)
        )
        row = sl_cur.fetchone()
        if row and row[0] and not _sync_lock_is_stale(row[0]):
            return False  # a live sync holds it

        # Upsert rather than INSERT OR REPLACE: the latter is SQLite-only syntax.
        # ON CONFLICT ... DO UPDATE is spelled identically on both current
        # backends, so this one statement serves either local store.
        now_iso = datetime.now(timezone.utc).isoformat()
        sl_cur.execute(
            f"INSERT INTO sync_locks (lock_name, holder, acquired_at) "
            f"VALUES ({_LOCAL_PARAM}, {_LOCAL_PARAM}, {_LOCAL_PARAM}) "
            f"ON CONFLICT (lock_name) DO UPDATE SET "
            f"holder = excluded.holder, acquired_at = excluded.acquired_at",
            (_SYNC_LOCK_NAME, _lock_value(now_iso), now_iso)
        )
        return True
    except Exception as e:
        logger.warning(f"Lock acquisition failed: {e}")
        return False


def _release_sync_lock(sl_cur):
    """Releases the global sync lock."""
    try:
        sl_cur.execute(
            f"DELETE FROM sync_locks WHERE lock_name = {_LOCAL_PARAM}",
            (_SYNC_LOCK_NAME,)
        )
    except Exception as e:
        logger.warning(f"Lock release failed: {e}")


# ── Generic manifest-driven sync ─────────────────────────────────────────────

def _sync_table_generic(
    sl_cur,
    pg_cur,
    sl_conn,
    target_name: str,
    table_cfg: dict[str, Any],
    dry_run: bool = False,
) -> None:
    """Generic bidirectional UPSERT for any table described in a manifest entry.

    Supports:
    - Composite PKs via pk_columns list
    - Custom tombstone columns (is_deleted bool or arbitrary string values)
    - Nullable timestamp_column (falls back to full table scan)
    - --dry-run: logs what would sync without touching either DB
    """
    name = table_cfg["name"]
    pk_columns: list[str] = table_cfg.get("pk_columns", ["id"])
    ts_col: str | None = table_cfg.get("timestamp_column")
    # tombstone_col is read from config but not yet honored in this generic
    # path — the tasks table has bespoke tombstone handling in
    # sync_tasks_table. Keeping the read so manifests with tombstone_column
    # don't break the schema; underscore-prefix silences the unused-var lint.
    _tombstone_col: str | None = table_cfg.get("tombstone_column")  # noqa: F841 — placeholder for future generic-path support
    skip: bool = table_cfg.get("skip", False)

    if skip:
        logger.info(f"[{target_name}] Skipping {name} (marked skip=true in manifest)")
        return

    logger.info(f"[{target_name}] Synchronizing {name} (manifest-driven)...")
    now = datetime.now(timezone.utc).isoformat()

    push_key = f"{name}_push"
    pull_key = f"{name}_pull"

    # ── PUSH: SQLite → PG ────────────────────────────────────────────────────
    watermark = _get_watermark(sl_cur, push_key, target_name)

    try:
        if ts_col and watermark:
            # Retain rows whose ts_col is NULL: they never exceed a watermark and
            # would be dropped forever after the first full push. The upsert guard
            # below makes a re-push idempotent, so the only cost is a little
            # redundant traffic — far better than permanent row loss.
            sl_cur.execute(
                f"SELECT * FROM {name} WHERE {ts_col} > {_LOCAL_PARAM} "
                f"OR {ts_col} IS NULL",
                (watermark,),
            )
            logger.info(f"[{target_name}] [{name}] Delta push: rows changed since {watermark}")
        else:
            sl_cur.execute(f"SELECT * FROM {name}")
            logger.info(f"[{target_name}] [{name}] Full push (no watermark or no timestamp col)")
    except Exception as exc:
        # Only a MISSING table/column is a skip: a manifest may legitimately name
        # a table this store does not have. Anything else — a locked database, a
        # corrupt page, a type error — must propagate, or a real fault is logged
        # as a skipped table and the sync reports success having moved nothing.
        if not _local_dialect().is_undefined_object_error(exc):
            raise
        logger.warning(f"[{target_name}] [{name}] Table/column missing locally: {exc}")
        return

    local_rows = sl_cur.fetchall()

    if dry_run:
        logger.info(f"[{target_name}] [{name}] [DRY-RUN] Would push {len(local_rows)} rows to PG")
    elif local_rows:
        try:
            # Get column names from cursor description
            col_names = [d[0] for d in sl_cur.description]
            conflict_clause = _build_conflict_clause(pk_columns)
            non_pk_cols = [c for c in col_names if c not in pk_columns]
            set_clause = ", ".join(f"{c} = EXCLUDED.{c}" for c in non_pk_cols) if non_pk_cols else "id = EXCLUDED.id"

            from psycopg2.extras import execute_values
            placeholders = ", ".join(["%s"] * len(col_names))
            col_list = ", ".join(col_names)

            upsert_pg = f"""
                INSERT INTO {name} ({col_list}) VALUES %s
                ON CONFLICT {conflict_clause} DO UPDATE SET
                    {set_clause}
            """
            # Add timestamp guard if we have a ts_col
            if ts_col and ts_col in col_names:
                upsert_pg += f" WHERE {name}.{ts_col} IS NULL OR EXCLUDED.{ts_col} > {name}.{ts_col}"

            push_count = 0
            for i in range(0, len(local_rows), BATCH_SIZE):
                batch = [tuple(r) for r in local_rows[i:i+BATCH_SIZE]]
                execute_values(pg_cur, upsert_pg, batch)
                push_count += len(batch)
            logger.info(f"[{target_name}] [{name}] Pushed {push_count} rows to PG")
        except Exception as exc:
            logger.error(f"[{target_name}] [{name}] Push failed: {type(exc).__name__}: {exc}")

    if not dry_run:
        _set_watermark(sl_cur, push_key, now, target_name)
        sl_conn.commit()

    # ── PULL: PG → SQLite ────────────────────────────────────────────────────
    watermark = _get_watermark(sl_cur, pull_key, target_name)

    if dry_run:
        logger.info(f"[{target_name}] [{name}] [DRY-RUN] Would pull rows from PG (watermark={watermark})")
    else:
        try:
            # Capture the PG SERVER clock BEFORE the pull, and use it (not the
            # local host clock) as the new pull watermark. The delta compares the
            # watermark against PG-side timestamps written by other peers' clocks;
            # a local `now` under clock skew can advance past rows PG hasn't caught
            # up to and silently drop them. Capturing NOW() *before* the pull also
            # avoids missing rows committed during the pull window.
            try:
                pg_cur.execute("SELECT NOW()")
                _pg_now_row = pg_cur.fetchone()
                pull_watermark = _pg_now_row[0].isoformat() if _pg_now_row else now
            except Exception:
                pull_watermark = now  # fall back to local clock if SELECT NOW() fails
            if ts_col and watermark:
                # Retain NULL-ts rows (see push side): otherwise they're never
                # pulled after the first full pull. Idempotent via the upsert guard.
                pg_cur.execute(
                    f"SELECT * FROM {name} WHERE {ts_col} > %s OR {ts_col} IS NULL",
                    (watermark,),
                )
                logger.info(f"[{target_name}] [{name}] Delta pull: rows changed since {watermark}")
            else:
                pg_cur.execute(f"SELECT * FROM {name}")
                logger.info(f"[{target_name}] [{name}] Full pull (no watermark or no timestamp col)")

            remote_rows = pg_cur.fetchall()
            pull_count = 0

            if remote_rows:
                col_names = [d[0] for d in pg_cur.description]
                conflict_clause = _build_conflict_clause(pk_columns)
                non_pk_cols = [c for c in col_names if c not in pk_columns]
                set_clause = ", ".join(f"{c} = excluded.{c}" for c in non_pk_cols) if non_pk_cols else "id = excluded.id"
                placeholders = ", ".join(["?"] * len(col_names))
                col_list = ", ".join(col_names)

                upsert_sl = f"""
                    INSERT INTO {name} ({col_list})
                    VALUES ({placeholders})
                    ON CONFLICT {conflict_clause} DO UPDATE SET
                        {set_clause}
                """
                if ts_col and ts_col in col_names:
                    upsert_sl += f" WHERE {name}.{ts_col} IS NULL OR excluded.{ts_col} > {name}.{ts_col}"

                for i in range(0, len(remote_rows), BATCH_SIZE):
                    batch = [tuple(r) for r in remote_rows[i:i+BATCH_SIZE]]
                    sl_cur.executemany(upsert_sl, batch)
                    pull_count += len(batch)
                    sl_conn.commit()

            logger.info(f"[{target_name}] [{name}] Pulled {pull_count} rows from PG")
            _set_watermark(sl_cur, pull_key, pull_watermark, target_name)
            sl_conn.commit()

        except Exception as exc:
            logger.error(f"[{target_name}] [{name}] Pull failed: {type(exc).__name__}: {exc}")


# ── Per-DB sync dispatcher ───────────────────────────────────────────────────

def _sync_agent_memory_db(sl_cur, pg_cur, sl_conn, target_name: str, dry_run: bool = False) -> None:
    """Sync agent_memory.db using the legacy per-table functions.

    This path is kept verbatim to preserve existing test coverage and
    specialised logic (FK pre-filter for embeddings, change_agent guard for
    memory_items, version-based conflict resolution for secrets).
    """
    if dry_run:
        logger.info(f"[{target_name}] [DRY-RUN] Would sync: memory_items, memory_embeddings, "
                    f"memory_relationships, tasks, synchronized_secrets")
        return

    # Step 1: Memory Items
    try:
        pg_cur.execute("SAVEPOINT items")
        sync_memory_items(sl_cur, pg_cur, sl_conn, target_name)
        pg_cur.execute("RELEASE SAVEPOINT items")
    except Exception as e:
        pg_cur.execute("ROLLBACK TO SAVEPOINT items")
        logger.error(f"[{target_name}] Memory items sync failed: {e}")

    # Step 2: PG Tier Tables (Main only)
    if target_name == "main":
        try:
            _ensure_pg_tier_tables(pg_cur)
        except Exception as e:
            logger.warning(f"Ensuring PG tier tables failed: {e}")

    # Step 3: Relationships
    if _table_exists(sl_cur, "memory_relationships"):
        try:
            pg_cur.execute("SAVEPOINT rels")
            sync_memory_relationships(sl_cur, pg_cur, sl_conn, target_name)
            pg_cur.execute("RELEASE SAVEPOINT rels")
        except Exception as e:
            pg_cur.execute("ROLLBACK TO SAVEPOINT rels")
            logger.error(f"[{target_name}] Relationships sync failed: {e}")

    # Step 4: Embeddings
    if _table_exists(sl_cur, "memory_embeddings"):
        try:
            pg_cur.execute("SAVEPOINT embs")
            sync_memory_embeddings(sl_cur, pg_cur, sl_conn, target_name)
            pg_cur.execute("RELEASE SAVEPOINT embs")
        except Exception as e:
            pg_cur.execute("ROLLBACK TO SAVEPOINT embs")
            logger.error(f"[{target_name}] Embeddings sync failed: {e}")

    # Step 5: Tasks (If exists)
    if _table_exists(sl_cur, "tasks"):
        try:
            pg_cur.execute("SAVEPOINT tasks")
            sync_tasks(sl_cur, pg_cur, sl_conn, target_name)
            pg_cur.execute("RELEASE SAVEPOINT tasks")
        except Exception as e:
            pg_cur.execute("ROLLBACK TO SAVEPOINT tasks")
            logger.error(f"[{target_name}] Tasks sync failed: {e}")

    # Step 6: Secrets (If exists)
    if _table_exists(sl_cur, "synchronized_secrets"):
        try:
            sync_secrets(sl_cur, pg_cur, target_name)
        except Exception as e:
            logger.warning(f"[{target_name}] Secrets sync failed: {e}")


def _sync_generic_db(sl_cur, pg_cur, sl_conn, manifest: dict[str, Any],
                     target_name: str, dry_run: bool = False) -> None:
    """Sync any DB using manifest-driven generic sync loop."""
    table_map = manifest["_table_map"]
    sync_order: list[str] = manifest["sync_order"]

    for table_name in sync_order:
        if table_name not in table_map:
            logger.warning(f"[{target_name}] Table '{table_name}' in sync_order not found in tables list")
            continue

        table_cfg = table_map[table_name]
        if table_cfg.get("skip", False):
            logger.info(f"[{target_name}] Skipping {table_name} (skip=true in manifest)")
            continue

        if not _table_exists(sl_cur, table_name):
            logger.info(f"[{target_name}] Table {table_name} not present in SQLite DB — skipping")
            continue

        try:
            if not dry_run:
                pg_cur.execute(f"SAVEPOINT tbl_{table_name}")
            _sync_table_generic(sl_cur, pg_cur, sl_conn, target_name, table_cfg, dry_run=dry_run)
            if not dry_run:
                pg_cur.execute(f"RELEASE SAVEPOINT tbl_{table_name}")
        except Exception as e:
            if not dry_run:
                pg_cur.execute(f"ROLLBACK TO SAVEPOINT tbl_{table_name}")
            logger.error(f"[{target_name}] Table {table_name} sync failed: {e}")


# ── Sync targets ──────────────────────────────────────────────────────────────
#
# A "target" is one (STORE, TABLE-SET) pair to sync — deliberately not a file
# path, because the same deployment has a different SHAPE on each backend:
#
#   SQLite    TWO stores, each using the CORE table names. The chatlog is a
#             separate .db file that runs its own migration chain, so
#             agent_memory.db and agent_chatlog.db both contain `memory_items`.
#   Postgres  ONE store holding BOTH families: core `memory_items` plus the
#             chatlog clones `chat_log_items`, `chat_log_embeddings`, ... in the
#             same schema (see dialect._CHATLOG_TABLES, which states the rule and
#             keys off an explicit `backend == "sqlite"` predicate so a third
#             backend lands on the chat_log_* names with no edit).
#
# Iterating FILES therefore has no meaning on PostgreSQL, and a port that kept
# the file loop would not crash — it would sync the wrong table set, silently.
# That is why this resolves from the BACKEND rather than the filesystem.

class SyncTarget:
    """One (store, table-set) pair. `uri` is a path on SQLite, a DSN on PG."""

    __slots__ = ("name", "uri", "table_map")

    def __init__(self, name: str, uri: "str | None", table_map: "dict[str, str]"):
        self.name = name
        self.uri = uri
        # role -> physical table name in THIS store, e.g. {"items": "chat_log_items"}
        self.table_map = table_map

    def table(self, role: str) -> str:
        """The physical table for a logical role, or the role itself if unmapped."""
        return self.table_map.get(role, role)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"SyncTarget(name={self.name!r}, uri={self.uri!r})"


_CORE_ROLES = ("items", "embeddings", "relationships")


def resolve_sync_targets(main_uri: "str | None" = None) -> "list[SyncTarget]":
    """The stores and table-sets to sync, resolved from the ACTIVE backend.

    SQLite yields up to two targets (main + chatlog file) both using core table
    names; PostgreSQL yields ONE target covering core names AND the chat_log_*
    clones. A third backend inherits the PG shape via `chatlog_table_for`, which
    already keys on `backend == "sqlite"` rather than assuming everything else is
    Postgres.

    Never raises: a chatlog store that cannot be resolved is simply absent from
    the list. Skipping a store we cannot find is right; refusing to sync the one
    we CAN find because of it is not.
    """
    from memory.backends.dialect import chatlog_table_for

    backend = _local_backend()
    # The CORE table names, whatever the backend. chatlog_table_for(role,
    # "sqlite") returns them by construction (SQLite's chatlog reuses the core
    # names in a separate file), so this asks the mapper rather than restating
    # "memory_items" here — one source for the naming, per §2.
    core_map = {r: chatlog_table_for(r, "sqlite") for r in _CORE_ROLES}

    if backend.name != "sqlite":
        # ONE store, TWO families: the chatlog rows live beside the core ones
        # under chat_log_* names, so a single connection covers both.
        chat_map = {r: chatlog_table_for(r, backend.name) for r in _CORE_ROLES}
        return [
            SyncTarget("main", main_uri, core_map),
            SyncTarget("chatlog", main_uri, chat_map),
        ]

    # SQLite: separate FILES, each using the core table names.
    out: "list[SyncTarget]" = []
    try:
        import migrate_memory

        for t in migrate_memory.targets("all"):
            out.append(SyncTarget(t.name, t.db_path, dict(core_map)))
    except Exception as exc:  # noqa: BLE001 — fall back to the single main store
        logger.warning(f"Could not enumerate SQLite sync targets ({exc}); using main only.")
        out = [SyncTarget("main", main_uri, dict(core_map))]
    return out


# ── main() ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Bidirectional SQLite ↔ PostgreSQL sync with per-DB manifests.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to the SQLite database to sync. Default: the SDK-resolved "
             "canonical path (M3_DATABASE env / engine root / populated legacy "
             "store) — never a hardcoded repo-relative guess.",
    )
    parser.add_argument(
        "--manifest",
        default=None,
        help="Path to sync manifest YAML. Inferred from --db basename if omitted.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would sync without touching either database.",
    )
    args = parser.parse_args()

    # Single source of truth for "where the DB lives": defer to the SDK resolver
    # so the pre-flight existence check below targets the same populated DB the
    # actual sync uses (migrate_memory.targets() also resolves via resolve_db_path).
    # A hardcoded repo-relative default would fail the check on a populated engine/
    # install and abort the sync before it ever ran — the M3_MEMORY_ROOT drift.
    db_path = resolve_db_path(args.db)  # explicit --db wins; else canonical resolution
    db_stem = pathlib.Path(db_path).stem  # e.g. "agent_memory"

    # Resolve manifest
    manifest_path = args.manifest or _infer_manifest_path(db_path)
    manifest_path = os.path.abspath(manifest_path)

    if not os.path.exists(manifest_path):
        logger.error(f"Manifest not found: {manifest_path}")
        sys.exit(1)

    logger.info(f"pg_sync starting: db={db_path} manifest={manifest_path} dry_run={args.dry_run}")

    try:
        manifest = _load_manifest(manifest_path)
    except Exception as exc:
        logger.error(f"Failed to load manifest {manifest_path}: {exc}")
        sys.exit(1)

    if args.dry_run and not os.path.exists(db_path):
        logger.info(f"[DRY-RUN] SQLite DB not found at {db_path} — would skip")
        return

    if not os.path.exists(db_path):
        logger.error(f"SQLite DB not found: {db_path}")
        sys.exit(1)

    # For agent_memory.db, use the legacy multi-target path (preserves current behaviour).
    # For any other DB, use the manifest-driven generic path with a single target.
    is_agent_memory = (db_stem == "agent_memory")

    try:
        if is_agent_memory:
            # Legacy path: mirrors original main() with migrate_memory.targets("all")
            targets = migrate_memory.targets("all")
            logger.info(f"Starting synchronization for {len(targets)} targets: {[t.name for t in targets]}")

            if args.dry_run:
                for target in targets:
                    logger.info(f"[DRY-RUN] Would sync target {target.name} ({target.db_path})")
                    logger.info("[DRY-RUN] Tables: memory_items, memory_embeddings, "
                                "memory_relationships, tasks, synchronized_secrets")
                return

            with ctx.pg_connection() as pg_conn:
                pg_conn.autocommit = False
                _set_warehouse_search_path(pg_conn)

                for target in targets:
                    logger.info(f"--- Synchronizing target: {target.name} ({target.db_path}) ---")
                    # NEVER let sqlite3.connect CREATE the local store. It happily
                    # creates a missing file, and the sync then reads zero rows,
                    # writes zero rows, and reports success — a silent no-op on a
                    # replication path. main() gates the primary db_path at :1359,
                    # but these per-TARGET paths (e.g. agent_chatlog.db) were never
                    # checked, so an absent chatlog store was manufactured empty
                    # and "synced".
                    if not os.path.exists(target.db_path):
                        logger.warning(
                            f"[{target.name}] local store not present at "
                            f"{target.db_path} — skipping (refusing to create it)."
                        )
                        continue
                    try:
                        sl_conn = sqlite3.connect(target.db_path, timeout=30)
                        sl_conn.row_factory = sqlite3.Row
                    except Exception as e:
                        logger.error(f"Failed to connect to local DB {target.db_path}: {e}")
                        continue

                    try:
                        sl_cur = sl_conn.cursor()
                        _ensure_watermark_table(sl_cur)

                        if target.name == "main":
                            if not _acquire_sync_lock(sl_cur):
                                logger.warning("Another sync is already in progress (main lock found). Skipping.")
                                sl_conn.close()
                                return
                            sl_conn.commit()

                        with pg_conn.cursor() as pg_cur:
                            _sync_agent_memory_db(sl_cur, pg_cur, sl_conn, target.name, dry_run=False)

                        pg_conn.commit()
                        sl_conn.commit()
                        logger.info(f"Target '{target.name}' synchronization completed.")

                        if target.name == "main":
                            _release_sync_lock(sl_cur)
                            sl_conn.commit()

                    except Exception as e:
                        logger.error(f"Failed during sync of target {target.name}: {e}")
                    finally:
                        sl_conn.close()

        else:
            # Generic manifest-driven path for bench DBs and future additions
            target_name = db_stem
            logger.info(f"Starting manifest-driven sync for {db_stem} (manifest: {manifest_path})")

            if args.dry_run:
                table_map = manifest["_table_map"]
                sync_order = manifest["sync_order"]
                active = [t for t in sync_order if not table_map.get(t, {}).get("skip", False)]
                skipped = [t for t in sync_order if table_map.get(t, {}).get("skip", False)]
                logger.info(f"[DRY-RUN] Would sync tables: {active}")
                if skipped:
                    logger.info(f"[DRY-RUN] Would skip tables: {skipped}")
                # Open SQLite to show row counts
                try:
                    sl_conn = sqlite3.connect(db_path, timeout=30)
                    sl_conn.row_factory = sqlite3.Row
                    sl_cur = sl_conn.cursor()
                    for tname in active:
                        if _table_exists(sl_cur, tname):
                            sl_cur.execute(f"SELECT COUNT(*) FROM {tname}")
                            cnt = sl_cur.fetchone()[0]
                            logger.info(f"[DRY-RUN] [{tname}] {cnt} rows in SQLite")
                        else:
                            logger.info(f"[DRY-RUN] [{tname}] not present in SQLite DB")
                    sl_conn.close()
                except Exception as e:
                    logger.warning(f"[DRY-RUN] Could not open {db_path} for row counts: {e}")
                return

            try:
                sl_conn = sqlite3.connect(db_path, timeout=30)
                sl_conn.row_factory = sqlite3.Row
            except Exception as e:
                logger.error(f"Failed to connect to local DB {db_path}: {e}")
                sys.exit(1)

            try:
                sl_cur = sl_conn.cursor()
                _ensure_watermark_table(sl_cur)
                with ctx.pg_connection() as pg_conn:
                    pg_conn.autocommit = False
                    _set_warehouse_search_path(pg_conn)
                    with pg_conn.cursor() as pg_cur:
                        _sync_generic_db(sl_cur, pg_cur, sl_conn, manifest, target_name, dry_run=False)
                    pg_conn.commit()
                sl_conn.commit()
                logger.info(f"Manifest-driven sync for {db_stem} completed.")
            except Exception as e:
                logger.error(f"Sync failed for {db_stem}: {type(e).__name__}: {e}")
                sys.exit(1)
            finally:
                sl_conn.close()

        logger.info("pg_sync completed successfully.")

    except Exception as e:
        logger.error(f"PG Sync failed: {type(e).__name__}: {e}")
        sys.exit(1)


if __name__ == "__main__":
    ensure_venv()
    main()
