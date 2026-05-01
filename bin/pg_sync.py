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
from m3_sdk import resolve_venv_python


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
from m3_sdk import M3Context

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
    """Resolve PostgreSQL connection URL from environment or encrypted vault."""
    url = os.getenv("PG_URL", "").strip()
    if url:
        return url
    url = ctx.get_secret("PG_URL")
    if url:
        return url
    logger.error("PG_URL not found. Use `bin/auth_utils.py` to set it.")
    sys.exit(1)


# ── Watermarks ───────────────────────────────────────────────────────────────

def _get_watermark(sl_cur, direction: str, target_name: str) -> str | None:
    """Read last sync watermark for a direction and target database."""
    # Prefix direction with target_name for separate watermarks per DB
    prefixed_direction = f"{target_name}_{direction}" if target_name != "main" else direction
    try:
        sl_cur.execute("SELECT last_synced_at FROM sync_watermarks WHERE direction = ?", (prefixed_direction,))
        row = sl_cur.fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None


def _set_watermark(sl_cur, direction: str, ts: str, target_name: str) -> None:
    """Update last sync watermark for a direction and target database."""
    prefixed_direction = f"{target_name}_{direction}" if target_name != "main" else direction
    try:
        sl_cur.execute(
            """INSERT INTO sync_watermarks (direction, last_synced_at)
               VALUES (?, ?)
               ON CONFLICT(direction) DO UPDATE SET last_synced_at = excluded.last_synced_at""",
            (prefixed_direction, ts),
        )
    except sqlite3.OperationalError:
        pass


# ── Helpers ───────────────────────────────────────────────────────────────────

def _table_exists(sl_cur, table_name: str) -> bool:
    """Check if a table exists in the local SQLite DB."""
    sl_cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table_name,))
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
        sl_cur.execute("""
            SELECT id, type, title, content, metadata_json, agent_id, model_id,
                   change_agent, importance, source, origin_device, is_deleted,
                   expires_at, decay_rate, created_at, updated_at,
                   COALESCE(user_id, '') as user_id, COALESCE(scope, 'agent') as scope,
                   COALESCE(valid_from, '') as valid_from, COALESCE(valid_to, '') as valid_to, COALESCE(content_hash, '') as content_hash
            FROM memory_items
            WHERE updated_at > ? OR (updated_at IS NULL AND created_at > ?)
        """, (watermark, watermark))
        logger.info(f"[{target_name}] Delta push: rows changed since {watermark}")
    else:
        sl_cur.execute("""
            SELECT id, type, title, content, metadata_json, agent_id, model_id,
                   change_agent, importance, source, origin_device, is_deleted,
                   expires_at, decay_rate, created_at, updated_at,
                   COALESCE(user_id, '') as user_id, COALESCE(scope, 'agent') as scope,
                   COALESCE(valid_from, '') as valid_from, COALESCE(valid_to, '') as valid_to, COALESCE(content_hash, '') as content_hash
            FROM memory_items
        """)
        logger.info(f"[{target_name}] Full push: no watermark found (first sync)")

    local_rows = sl_cur.fetchall()
    push_count = 0
    push_errors = 0

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
    if watermark:
        pg_cur.execute("""
            SELECT id, type, title, content, metadata_json, agent_id, model_id,
                   change_agent, importance, source, origin_device, is_deleted,
                   expires_at, decay_rate, created_at, updated_at,
                   COALESCE(user_id, '') as user_id, COALESCE(scope, 'agent') as scope,
                   COALESCE(valid_from, '') as valid_from, COALESCE(valid_to, '') as valid_to, COALESCE(content_hash, '') as content_hash
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
                   COALESCE(valid_from, '') as valid_from, COALESCE(valid_to, '') as valid_to, COALESCE(content_hash, '') as content_hash
            FROM memory_items
        """)
        logger.info(f"[{target_name}] Full pull: no watermark found (first sync)")

    remote_rows = pg_cur.fetchall()
    pull_count = 0
    pull_errors = 0

    if remote_rows:
        try:
            # SQLite batch UPSERT
            upsert_query = """
                INSERT INTO memory_items (
                    id, type, title, content, metadata_json, agent_id, model_id,
                    change_agent, importance, source, origin_device, is_deleted,
                    expires_at, decay_rate, created_at, updated_at, user_id, scope,
                    valid_from, valid_to, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    title = excluded.title,
                    content = excluded.content,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at,
                    is_deleted = excluded.is_deleted,
                    change_agent = excluded.change_agent,
                    user_id = excluded.user_id,
                    scope = excluded.scope,
                    valid_from = excluded.valid_from,
                    valid_to = excluded.valid_to,
                    content_hash = excluded.content_hash
                WHERE (memory_items.updated_at IS NULL OR excluded.updated_at > memory_items.updated_at)
                  AND (
                      (memory_items.change_agent NOT IN ('manual', 'system'))
                      OR (excluded.change_agent = 'manual')
                  )
            """
            # Process in batches
            for i in range(0, len(remote_rows), BATCH_SIZE):
                batch = []
                for row in remote_rows[i:i+BATCH_SIZE]:
                    row_list = list(row)
                    if isinstance(row_list[4], dict):
                        row_list[4] = json.dumps(row_list[4])
                    batch.append(row_list)

                sl_cur.executemany(upsert_query, batch)
                pull_count += len(batch)
                sl_conn.commit()
        except Exception as exc:
            logger.error(f"[{target_name}] Batch pull failed: {type(exc).__name__}: {exc}")
            pull_errors = len(remote_rows)

    _set_watermark(sl_cur, "pg_pull", now, target_name)
    logger.info(f"[{target_name}] Pulled {pull_count} remote memory items ({pull_errors} errors).")


def sync_memory_relationships(sl_cur, pg_cur, sl_conn, target_name: str):
    """Synchronizes the memory_relationships table bi-directionally with watermark logic."""
    logger.info(f"[{target_name}] Synchronizing memory_relationships...")
    from psycopg2.extras import execute_values
    now = datetime.now(timezone.utc).isoformat()

    # 1. PUSH: Local to Remote
    watermark = _get_watermark(sl_cur, "rel_push", target_name)
    if watermark:
        sl_cur.execute("SELECT id, from_id, to_id, relationship_type, created_at FROM memory_relationships WHERE created_at > ?", (watermark,))
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
    if watermark:
        pg_cur.execute("SELECT id, from_id, to_id, relationship_type, created_at FROM memory_relationships WHERE created_at > %s", (watermark,))
    else:
        pg_cur.execute("SELECT id, from_id, to_id, relationship_type, created_at FROM memory_relationships")

    remote_rows = pg_cur.fetchall()
    pull_count = 0
    if remote_rows:
        try:
            sl_cur.executemany("INSERT INTO memory_relationships (id, from_id, to_id, relationship_type, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (id) DO NOTHING", remote_rows)
            pull_count = len(remote_rows)
            sl_conn.commit()
        except Exception as exc:
            logger.warning(f"[{target_name}] Batch Relationship pull failed: {type(exc).__name__}")

    _set_watermark(sl_cur, "rel_pull", now, target_name)
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
            sl_cur.executemany("""
                INSERT INTO synchronized_secrets (service_name, encrypted_value, version, origin_device, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (service_name) DO UPDATE SET
                    encrypted_value = excluded.encrypted_value,
                    version = excluded.version,
                    origin_device = excluded.origin_device,
                    updated_at = excluded.updated_at
                WHERE excluded.version > synchronized_secrets.version
                   OR (excluded.version = synchronized_secrets.version AND excluded.updated_at > synchronized_secrets.updated_at)
            """, remote_rows)
            pull_count = len(remote_rows)
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
            f"SELECT {task_cols} FROM tasks WHERE updated_at > ? OR (updated_at IS NULL AND created_at > ?)",
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
            upsert = f"""
                INSERT INTO tasks ({task_cols})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (id) DO UPDATE SET
                    title            = excluded.title,
                    description      = excluded.description,
                    state            = excluded.state,
                    owner_agent      = excluded.owner_agent,
                    parent_task_id   = excluded.parent_task_id,
                    result_memory_id = excluded.result_memory_id,
                    metadata_json    = excluded.metadata_json,
                    updated_at       = excluded.updated_at,
                    completed_at     = excluded.completed_at,
                    deleted_at       = excluded.deleted_at
                WHERE tasks.updated_at IS NULL OR excluded.updated_at > tasks.updated_at
            """
            for i in range(0, len(remote_rows), BATCH_SIZE):
                batch = [tuple(r) for r in remote_rows[i:i+BATCH_SIZE]]
                sl_cur.executemany(upsert, batch)
                pull_count += len(batch)
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


def sync_memory_embeddings(sl_cur, pg_cur, sl_conn, target_name: str):
    """Synchronizes the memory_embeddings table bi-directionally with watermark logic."""
    logger.info(f"[{target_name}] Synchronizing memory_embeddings...")
    from psycopg2 import Binary
    from psycopg2.extras import execute_values
    now = datetime.now(timezone.utc).isoformat()

    _ensure_pg_embeddings_schema(pg_cur)

    # 1. PUSH: Local to Remote (delta)
    watermark = _get_watermark(sl_cur, "emb_push", target_name)
    if watermark:
        # memory_embeddings has no updated_at, so filter by parent memory_item timestamps
        sl_cur.execute("""
            SELECT id, memory_id, embedding, embed_model, dim
            FROM memory_embeddings
            WHERE memory_id IN (
                SELECT id FROM memory_items
                WHERE updated_at > ? OR (updated_at IS NULL AND created_at > ?)
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
                    batch.append(row_list)

                sl_cur.executemany("""
                    INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT (id) DO UPDATE SET
                        embedding = excluded.embedding,
                        embed_model = excluded.embed_model,
                        dim = excluded.dim
                """, batch)
                pull_count += len(batch)
                sl_conn.commit()
        except Exception as exc:
            logger.error(f"[{target_name}] Batch embedding pull failed: {type(exc).__name__}: {exc}")
            pull_errors = len(remote_rows)

    _set_watermark(sl_cur, "emb_pull", now, target_name)
    sl_conn.commit()
    logger.info(f"[{target_name}] Pulled {pull_count} embeddings from warehouse ({pull_errors} errors).")


# ── Sync lock ─────────────────────────────────────────────────────────────────

def _acquire_sync_lock(sl_cur) -> bool:
    """Attempts to acquire a global sync lock. Returns True if successful."""
    try:
        # Check if lock exists and is not stale (stale after 1 hour)
        sl_cur.execute("SELECT last_pull_at FROM sync_state WHERE collection_name = 'pg_sync_lock'")
        row = sl_cur.fetchone()
        if row:
            last_lock = datetime.fromisoformat(row[0])
            if (datetime.now(timezone.utc) - last_lock).total_seconds() < 3600:
                return False

        sl_cur.execute(
            "INSERT OR REPLACE INTO sync_state (collection_name, last_pull_at) VALUES ('pg_sync_lock', ?)",
            (datetime.now(timezone.utc).isoformat(),)
        )
        return True
    except Exception as e:
        logger.warning(f"Lock acquisition failed: {e}")
        return False


def _release_sync_lock(sl_cur):
    """Releases the global sync lock."""
    try:
        sl_cur.execute("DELETE FROM sync_state WHERE collection_name = 'pg_sync_lock'")
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
            sl_cur.execute(
                f"SELECT * FROM {name} WHERE {ts_col} > ?", (watermark,)
            )
            logger.info(f"[{target_name}] [{name}] Delta push: rows changed since {watermark}")
        else:
            sl_cur.execute(f"SELECT * FROM {name}")
            logger.info(f"[{target_name}] [{name}] Full push (no watermark or no timestamp col)")
    except sqlite3.OperationalError as exc:
        logger.warning(f"[{target_name}] [{name}] Cannot query SQLite: {exc}")
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
            if ts_col and watermark:
                pg_cur.execute(
                    f"SELECT * FROM {name} WHERE {ts_col} > %s", (watermark,)
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
            _set_watermark(sl_cur, pull_key, now, target_name)
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


# ── main() ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Bidirectional SQLite ↔ PostgreSQL sync with per-DB manifests.",
    )
    parser.add_argument(
        "--db",
        default=os.path.join(BASE_DIR, "memory", "agent_memory.db"),
        help="Path to the SQLite database to sync (default: memory/agent_memory.db)",
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

    db_path = os.path.abspath(args.db)
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

                for target in targets:
                    logger.info(f"--- Synchronizing target: {target.name} ({target.db_path}) ---")
                    try:
                        sl_conn = sqlite3.connect(target.db_path, timeout=30)
                        sl_conn.row_factory = sqlite3.Row
                    except Exception as e:
                        logger.error(f"Failed to connect to local DB {target.db_path}: {e}")
                        continue

                    try:
                        sl_cur = sl_conn.cursor()

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
                with ctx.pg_connection() as pg_conn:
                    pg_conn.autocommit = False
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
