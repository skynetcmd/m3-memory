from __future__ import annotations

import sys
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))
from m3_sdk import resolve_venv_python

def ensure_venv():
    venv_python = resolve_venv_python()
    if os.path.exists(venv_python) and sys.executable != venv_python:
        os.execl(venv_python, venv_python, *sys.argv)

ensure_venv()

import sqlite3
import logging
import json
from datetime import datetime, timezone
from m3_sdk import M3Context

# Python 3.12+ sqlite3 datetime adapter deprecation fix
sqlite3.register_adapter(datetime, lambda val: val.isoformat())

logging.basicConfig(level=logging.INFO, format='%(name)s: [%(levelname)s] %(message)s')
logger = logging.getLogger("pg_sync")

# Initialize SDK context
ctx = M3Context()
DB_PATH = ctx.db_path

BATCH_SIZE = 100  # commit every N rows

# Exclude benchmark / test agent data from warehouse pushes.
# These prefixes match agent_ids created by test_memory_bridge.py,
# benchmark_memory.py, and test_debug_agent.py.
_TEST_AGENT_FILTER = """
    AND agent_id NOT LIKE 'bench_%'
    AND agent_id NOT LIKE 'test_%'
    AND agent_id NOT LIKE 'cons_%'
    AND agent_id NOT LIKE 'import_test_%'
    AND agent_id NOT IN ('notif-test', 'agent-X', 'test-agent-B')
"""


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


def _get_watermark(sl_cur, direction: str) -> str | None:
    """Read last sync watermark for a direction ('pg_push' or 'pg_pull')."""
    try:
        sl_cur.execute("SELECT last_synced_at FROM sync_watermarks WHERE direction = ?", (direction,))
        row = sl_cur.fetchone()
        return row[0] if row else None
    except sqlite3.OperationalError:
        return None


def _set_watermark(sl_cur, direction: str, ts: str) -> None:
    """Update last sync watermark.

    Note: Watermark updates are NOT atomic with the data push/pull they follow.
    A crash between data write and watermark update could cause duplicate rows
    on next sync. This is safe because all sync operations use UPSERT
    (ON CONFLICT DO UPDATE), providing at-least-once delivery semantics.
    """
    try:
        sl_cur.execute(
            """INSERT INTO sync_watermarks (direction, last_synced_at)
               VALUES (?, ?)
               ON CONFLICT(direction) DO UPDATE SET last_synced_at = excluded.last_synced_at""",
            (direction, ts),
        )
    except sqlite3.OperationalError:
        pass


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


def sync_memory_items(sl_cur, pg_cur, sl_conn):
    logger.info("Synchronizing memory_items (UUID-based, delta sync)...")
    _ensure_pg_schema(pg_cur)
    now = datetime.now(timezone.utc).isoformat()

    # 1. PUSH: Local to Remote (delta — only changed rows since last push)
    watermark = _get_watermark(sl_cur, "pg_push")
    _SELECT_COLS = """
            SELECT id, type, title, content, metadata_json, agent_id, model_id,
                   change_agent, importance, source, origin_device, is_deleted,
                   expires_at, decay_rate, created_at, updated_at,
                   COALESCE(user_id, '') as user_id, COALESCE(scope, 'agent') as scope,
                   COALESCE(valid_from, '') as valid_from, COALESCE(valid_to, '') as valid_to, COALESCE(content_hash, '') as content_hash
            FROM memory_items
            WHERE 1=1 """ + _TEST_AGENT_FILTER

    if watermark:
        sl_cur.execute(_SELECT_COLS + """
            AND (updated_at > ? OR (updated_at IS NULL AND created_at > ?))
        """, (watermark, watermark))
        logger.info(f"Delta push: rows changed since {watermark}")
    else:
        sl_cur.execute(_SELECT_COLS)
        logger.info("Full push: no watermark found (first sync)")

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
            logger.error(f"Batch push failed: {type(exc).__name__}: {exc}")
            push_errors = len(local_rows)

    _set_watermark(sl_cur, "pg_push", now)
    sl_conn.commit()
    logger.info(f"Pushed {push_count} local memory items ({push_errors} errors).")

    # 2. PULL: Remote to Local (delta — only changed rows since last pull)
    watermark = _get_watermark(sl_cur, "pg_pull")
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
        logger.info(f"Delta pull: rows changed since {watermark}")
    else:
        pg_cur.execute("""
            SELECT id, type, title, content, metadata_json, agent_id, model_id,
                   change_agent, importance, source, origin_device, is_deleted,
                   expires_at, decay_rate, created_at, updated_at,
                   COALESCE(user_id, '') as user_id, COALESCE(scope, 'agent') as scope,
                   COALESCE(valid_from, '') as valid_from, COALESCE(valid_to, '') as valid_to, COALESCE(content_hash, '') as content_hash
            FROM memory_items
        """)
        logger.info("Full pull: no watermark found (first sync)")

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
            logger.error(f"Batch pull failed: {type(exc).__name__}: {exc}")
            pull_errors = len(remote_rows)

    _set_watermark(sl_cur, "pg_pull", now)
    logger.info(f"Pulled {pull_count} remote memory items ({pull_errors} errors).")


def sync_memory_relationships(sl_cur, pg_cur, sl_conn):
    """Synchronizes the memory_relationships table bi-directionally with watermark logic."""
    logger.info("Synchronizing memory_relationships...")
    from psycopg2.extras import execute_values
    now = datetime.now(timezone.utc).isoformat()

    # 1. PUSH: Local to Remote
    watermark = _get_watermark(sl_cur, "rel_push")
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
            logger.warning(f"Batch Relationship push failed: {type(exc).__name__}")

    _set_watermark(sl_cur, "rel_push", now)
    sl_conn.commit()
    logger.info(f"Pushed {push_count} relationships to warehouse.")

    # 2. PULL: Remote to Local
    watermark = _get_watermark(sl_cur, "rel_pull")
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
            logger.warning(f"Batch Relationship pull failed: {type(exc).__name__}")
    
    _set_watermark(sl_cur, "rel_pull", now)
    sl_conn.commit()
    logger.info(f"Pulled {pull_count} relationships from warehouse.")


def sync_secrets(sl_cur, pg_cur):
    logger.info("Synchronizing encrypted secrets vault...")
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
            logger.warning(f"Batch Secret push failed: {type(exc).__name__}")

    logger.info(f"Pushed {push_count} local secrets to the warehouse.")

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
            logger.warning(f"Batch Secret pull failed: {type(exc).__name__}")

    logger.info(f"Pulled {pull_count} remote secrets from the warehouse.")


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


def sync_tasks(sl_cur, pg_cur, sl_conn):
    """Bi-directional delta sync for the tasks table, including soft-delete tombstones.

    Tombstones ride through as ordinary UPSERTs: deleted_at is just a column,
    and any change to it bumps updated_at, so delta watermarks pick it up for
    free. Sync is UPSERT-only (no DELETE propagation) — cross-peer hard-delete
    is out of scope; peers converge via tombstones.
    """
    logger.info("Synchronizing tasks...")
    from psycopg2.extras import execute_values
    now = datetime.now(timezone.utc).isoformat()

    _ensure_pg_tasks_schema(pg_cur)

    task_cols = (
        "id, title, description, state, owner_agent, created_by, parent_task_id, "
        "result_memory_id, metadata_json, created_at, updated_at, completed_at, deleted_at"
    )

    # 1. PUSH: local → remote (delta on updated_at, excluding test data).
    # Tombstones always ride through so deletes propagate even for test-agent rows
    # that were pushed before the test-agent filter existed.
    _task_filter = _TEST_AGENT_FILTER.replace("agent_id", "created_by")
    _task_base = (
        f"SELECT {task_cols} FROM tasks WHERE (deleted_at IS NOT NULL OR (1=1 "
        + _task_filter
        + "))"
    )
    watermark = _get_watermark(sl_cur, "tasks_push")
    if watermark:
        sl_cur.execute(
            _task_base + " AND (updated_at > ? OR (updated_at IS NULL AND created_at > ?))",
            (watermark, watermark),
        )
        logger.info(f"Delta task push: rows changed since {watermark}")
    else:
        sl_cur.execute(_task_base)
        logger.info("Full task push: no watermark found (first sync)")

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
            logger.error(f"Batch task push failed: {type(exc).__name__}: {exc}")

    _set_watermark(sl_cur, "tasks_push", now)
    sl_conn.commit()
    logger.info(f"Pushed {push_count} tasks to warehouse.")

    # 2. PULL: remote → local (delta on updated_at)
    watermark = _get_watermark(sl_cur, "tasks_pull")
    if watermark:
        pg_cur.execute(
            f"SELECT {task_cols} FROM tasks WHERE updated_at > %s OR (updated_at IS NULL AND created_at > %s)",
            (watermark, watermark),
        )
        logger.info(f"Delta task pull: rows changed since {watermark}")
    else:
        pg_cur.execute(f"SELECT {task_cols} FROM tasks")
        logger.info("Full task pull: no watermark found (first sync)")

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
            logger.error(f"Batch task pull failed: {type(exc).__name__}: {exc}")

    _set_watermark(sl_cur, "tasks_pull", now)
    sl_conn.commit()
    logger.info(f"Pulled {pull_count} tasks from warehouse.")


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


def sync_memory_embeddings(sl_cur, pg_cur, sl_conn):
    """Synchronizes the memory_embeddings table bi-directionally with watermark logic."""
    logger.info("Synchronizing memory_embeddings...")
    from psycopg2.extras import execute_values
    from psycopg2 import Binary
    now = datetime.now(timezone.utc).isoformat()

    _ensure_pg_embeddings_schema(pg_cur)

    # 1. PUSH: Local to Remote (delta)
    watermark = _get_watermark(sl_cur, "emb_push")
    _EMB_BASE = """
            SELECT me.id, me.memory_id, me.embedding, me.embed_model, me.dim
            FROM memory_embeddings me
            JOIN memory_items mi ON me.memory_id = mi.id
            WHERE 1=1 """ + _TEST_AGENT_FILTER.replace("agent_id", "mi.agent_id")

    if watermark:
        sl_cur.execute(_EMB_BASE + """
            AND (mi.updated_at > ? OR (mi.updated_at IS NULL AND mi.created_at > ?))
        """, (watermark, watermark))
        logger.info(f"Delta embedding push: rows changed since {watermark}")
    else:
        sl_cur.execute(_EMB_BASE)
        logger.info("Full embedding push: no watermark found (first sync)")

    local_rows = sl_cur.fetchall()
    push_count = 0
    push_errors = 0

    if local_rows:
        try:
            # Pre-filter: only push embeddings whose memory_id exists in PG memory_items.
            # This avoids FK violations when local items haven't been pushed yet.
            local_memory_ids = list({row[1] for row in local_rows})
            pg_existing_ids = set()
            for i in range(0, len(local_memory_ids), BATCH_SIZE):
                batch_ids = local_memory_ids[i:i+BATCH_SIZE]
                pg_cur.execute(
                    "SELECT id FROM memory_items WHERE id IN %s",
                    (tuple(batch_ids),),
                )
                pg_existing_ids.update(r[0] for r in pg_cur.fetchall())

            # Convert sqlite3.Row to tuples with Binary-wrapped embedding blobs
            values = []
            skipped = 0
            for row in local_rows:
                if row[1] not in pg_existing_ids:
                    skipped += 1
                    continue
                row_list = list(row)
                # row[2] is the embedding blob — wrap for PG BYTEA
                row_list[2] = Binary(row_list[2])
                values.append(tuple(row_list))

            if skipped:
                logger.info(f"Skipped {skipped} embeddings (memory_id not in PG memory_items)")

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
            logger.error(f"Batch embedding push failed: {type(exc).__name__}: {exc}")
            push_errors = len(local_rows)

    _set_watermark(sl_cur, "emb_push", now)
    sl_conn.commit()
    logger.info(f"Pushed {push_count} embeddings to warehouse ({push_errors} errors).")

    # 2. PULL: Remote to Local (delta)
    watermark = _get_watermark(sl_cur, "emb_pull")
    if watermark:
        pg_cur.execute("""
            SELECT me.id, me.memory_id, me.embedding, me.embed_model, me.dim
            FROM memory_embeddings me
            JOIN memory_items mi ON me.memory_id = mi.id
            WHERE mi.updated_at > %s OR (mi.updated_at IS NULL AND mi.created_at > %s)
        """, (watermark, watermark))
        logger.info(f"Delta embedding pull: rows changed since {watermark}")
    else:
        pg_cur.execute("SELECT id, memory_id, embedding, embed_model, dim FROM memory_embeddings")
        logger.info("Full embedding pull: no watermark found (first sync)")

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
            logger.error(f"Batch embedding pull failed: {type(exc).__name__}: {exc}")
            pull_errors = len(remote_rows)

    _set_watermark(sl_cur, "emb_pull", now)
    sl_conn.commit()
    logger.info(f"Pulled {pull_count} embeddings from warehouse ({pull_errors} errors).")


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

def main():
    try:
        logger.info("Connecting to local SQLite DB at %s...", DB_PATH)
        with ctx.get_sqlite_conn() as sl_conn:
            sl_cur = sl_conn.cursor()
            if not _acquire_sync_lock(sl_cur):
                logger.warning("Another sync is already in progress. Skipping.")
                return
            sl_conn.commit()

            try:
                logger.info("Connecting to data warehouse pool...")
                with ctx.pg_connection() as pg_conn:
                    pg_conn.autocommit = False
                    with pg_conn.cursor() as pg_cur:
                        sync_memory_items(sl_cur, pg_cur, sl_conn)
                        _ensure_pg_tier_tables(pg_cur)
                        sync_memory_relationships(sl_cur, pg_cur, sl_conn)
                        sync_memory_embeddings(sl_cur, pg_cur, sl_conn)
                        sync_tasks(sl_cur, pg_cur, sl_conn)
                        sync_secrets(sl_cur, pg_cur)

                    pg_conn.commit()

                sl_conn.commit()
                logger.info("Data warehouse synchronization completed successfully!")
            except Exception as e:
                logger.error(f"PG Sync Transaction failed: {type(e).__name__}: {e}")
                raise
            finally:
                _release_sync_lock(sl_cur)
                sl_conn.commit()

    except Exception as e:
        logger.error(f"Sync failed: {type(e).__name__}: {e}")
        sys.exit(1)
    

if __name__ == "__main__":
    main()
