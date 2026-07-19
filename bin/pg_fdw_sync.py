#!/usr/bin/env python3
"""pg_fdw_sync.py — PostgreSQL-primary -> PostgreSQL-warehouse fast-path sync.

When BOTH the primary store and the CDW warehouse are PostgreSQL, the row-by-row
SQLite<->PG bridge (pg_sync.py) is the wrong tool: it opens the local side as
sqlite3 and UPSERTs one row at a time in Python. This module uses postgres_fdw to
make the warehouse a set of foreign tables in the primary, so each table's delta
sync is a single server-side ``INSERT INTO <target> SELECT <cols> FROM <source>
WHERE updated_at > :wm ON CONFLICT (id) DO UPDATE ... WHERE target.updated_at <
EXCLUDED.updated_at`` — bulk, streaming, no Python row loop, last-writer-wins.

Directionality: both PUSH (primary -> warehouse) and PULL (warehouse -> primary)
run from the PRIMARY side (which hosts the FDW foreign tables). Push writes the
foreign table; pull reads it. Symmetric SQL, source/target swapped.

Scope: PG->PG only. SQLite source (the default install) and MariaDB stay on
pg_sync.py's generic bridge — sync_all dispatches by backend. This module NEVER
hard-fails the whole sync: if postgres_fdw is absent or the foreign server is
unreachable, it signals the caller to fall back to the generic path.

Requires (one-time, superuser on the PRIMARY pg): CREATE EXTENSION postgres_fdw.
Requires on the WAREHOUSE: the sync role has USAGE + DML on m3_warehouse.
"""
from __future__ import annotations

import logging
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("pg_fdw_sync")

# Local schema the warehouse foreign tables are imported into (kept distinct from
# the primary's own public.* so a table name never collides).
FDW_SCHEMA = "cdw_fdw"
FDW_SERVER = "cdw_wh"
WAREHOUSE_SCHEMA = "m3_warehouse"

# The synced column contract per table — the WAREHOUSE column set (a subset of the
# primary's; belief/KG columns stay local, matching the SQLite bridge). Keeping it
# explicit (not SELECT *) is REQUIRED: primary.memory_items has more columns than
# warehouse.memory_items, so a * would mismatch arity.
_MEMORY_ITEMS_COLS = [
    "id", "type", "title", "content", "metadata_json", "agent_id", "model_id",
    "change_agent", "importance", "source", "origin_device", "is_deleted",
    "expires_at", "decay_rate", "created_at", "updated_at", "last_accessed_at",
    "access_count", "user_id", "scope", "valid_from", "valid_to", "content_hash",
    "read_at", "conversation_id", "refresh_on", "refresh_reason", "variant",
]

# Per-table sync spec: (table, columns, pk, timestamp_col). Columns are the
# SHARED set (primary ∩ warehouse), schema-verified — NOT guessed. timestamp_col
# drives the delta watermark AND the last-writer-wins guard; None => full-scan
# id-keyed upsert (correct for tables whose shared columns lack updated_at:
# embeddings cascade-delete via memory_items, relationships are immutable).
_TABLE_SPECS = [
    ("memory_items", _MEMORY_ITEMS_COLS, "id", "updated_at"),
    ("memory_embeddings",
     ["id", "memory_id", "embedding", "embed_model", "dim", "created_at",
      "content_hash", "vector_kind"],
     "id", None),
    ("memory_relationships",
     ["id", "from_id", "to_id", "relationship_type", "created_at"],
     "id", None),
]


class FdwUnavailable(Exception):
    """Raised when the FDW fast-path can't run (no extension / unreachable server);
    the caller should fall back to the generic pg_sync bridge."""


def _ensure_fdw_wired(pg_cur, warehouse_dsn: str) -> None:
    """Idempotently set up postgres_fdw: extension, foreign server, user mapping,
    and IMPORT FOREIGN SCHEMA. Raises FdwUnavailable if the extension is missing
    (needs superuser to create) or the server can't be reached."""
    d = urllib.parse.urlparse(warehouse_dsn)
    host, port = d.hostname, str(d.port or 5432)
    dbname = d.path.lstrip("/")
    user, password = d.username, d.password

    # Extension — cannot self-create without superuser; probe rather than assume.
    pg_cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'postgres_fdw'")
    if pg_cur.fetchone() is None:
        raise FdwUnavailable(
            "postgres_fdw extension not installed on the primary "
            "(run `CREATE EXTENSION postgres_fdw;` as superuser)")

    # Foreign server + user mapping (idempotent). Server options can't be changed
    # by CREATE IF NOT EXISTS if they drift, but for our fixed warehouse they're
    # stable; a drop/recreate would invalidate imported tables, so keep IF NOT EXISTS.
    pg_cur.execute(
        f"CREATE SERVER IF NOT EXISTS {FDW_SERVER} FOREIGN DATA WRAPPER postgres_fdw "
        f"OPTIONS (host %s, dbname %s, port %s)", (host, dbname, port))
    # User mapping: recreate to pick up a rotated password. current_user maps to
    # the warehouse role. Omit the password option entirely when the DSN carries
    # none (e.g. trust/peer/.pgpass auth) — passing password=NULL is a SQL syntax
    # error, and an empty-string password is not the same as "no password".
    pg_cur.execute(f"DROP USER MAPPING IF EXISTS FOR CURRENT_USER SERVER {FDW_SERVER}")
    if password:
        pg_cur.execute(
            f"CREATE USER MAPPING FOR CURRENT_USER SERVER {FDW_SERVER} "
            f"OPTIONS (user %s, password %s)", (user, password))
    else:
        pg_cur.execute(
            f"CREATE USER MAPPING FOR CURRENT_USER SERVER {FDW_SERVER} "
            f"OPTIONS (user %s)", (user,))

    pg_cur.execute(f"CREATE SCHEMA IF NOT EXISTS {FDW_SCHEMA}")
    # (Re)import the warehouse tables as foreign tables. IMPORT FOREIGN SCHEMA is
    # not idempotent (errors if a foreign table already exists), so import into a
    # freshly-cleared schema each run — cheap, and picks up warehouse DDL changes.
    tables = ", ".join(t[0] for t in _TABLE_SPECS)
    pg_cur.execute(f"DROP SCHEMA IF EXISTS {FDW_SCHEMA} CASCADE")
    pg_cur.execute(f"CREATE SCHEMA {FDW_SCHEMA}")
    try:
        pg_cur.execute(
            f"IMPORT FOREIGN SCHEMA {WAREHOUSE_SCHEMA} LIMIT TO ({tables}) "
            f"FROM SERVER {FDW_SERVER} INTO {FDW_SCHEMA}")
    except Exception as e:
        # Connection / auth / permission failures surface here.
        raise FdwUnavailable(f"IMPORT FOREIGN SCHEMA failed: {e}") from e


def _upsert(pg_cur, target_qual: str, source_qual: str, cols: list[str], pk: str,
            ts_col: Optional[str], watermark: Optional[str]) -> int:
    """One set-based delta upsert. target/source are schema-qualified table names.
    Returns rows affected. Last-writer-wins on ts_col (when present)."""
    collist = ", ".join(cols)
    non_pk = [c for c in cols if c != pk]
    set_clause = ", ".join(f"{c}=EXCLUDED.{c}" for c in non_pk)
    where_delta = ""
    params: list = []
    if ts_col and watermark:
        where_delta = f" WHERE {ts_col} > %s"
        params.append(watermark)
    # Last-writer-wins guard only when we have a timestamp to compare.
    conflict_guard = (f" WHERE {target_qual}.{ts_col} < EXCLUDED.{ts_col}"
                      if ts_col else "")
    sql = (
        f"INSERT INTO {target_qual} ({collist}) "
        f"SELECT {collist} FROM {source_qual}{where_delta} "
        f"ON CONFLICT ({pk}) DO UPDATE SET {set_clause}{conflict_guard}"
    )
    pg_cur.execute(sql, params)
    return pg_cur.rowcount


def sync_pg_to_pg(primary_conn, warehouse_dsn: str, get_wm, set_wm,
                  dry_run: bool = False) -> dict:
    """Bidirectional PG<->PG warehouse sync via FDW, driven from the primary.

    primary_conn: an open psycopg connection to the PRIMARY (public.* store).
    warehouse_dsn: the CDW DSN (M3_CDW_PG_URL) — used for the foreign server.
    get_wm(direction) -> iso str | None ; set_wm(direction, iso) : watermark I/O
      (direction e.g. 'fdw_memory_items_push'). Reuse the caller's sync_watermarks.
    Returns {table: {push: n, pull: n}}. Raises FdwUnavailable -> caller falls back.
    """
    results: dict = {}
    with primary_conn.cursor() as cur:
        _ensure_fdw_wired(cur, warehouse_dsn)
        if dry_run:
            primary_conn.rollback()
            return {"dry_run": True, "tables": [t[0] for t in _TABLE_SPECS]}
        now = datetime.now(timezone.utc).isoformat()
        for table, cols, pk, ts_col in _TABLE_SPECS:
            local = f"public.{table}"
            foreign = f"{FDW_SCHEMA}.{table}"
            push_key, pull_key = f"fdw_{table}_push", f"fdw_{table}_pull"
            # PUSH: primary -> warehouse (write the foreign table)
            n_push = _upsert(cur, foreign, local, cols, pk, ts_col, get_wm(push_key))
            # PULL: warehouse -> primary (read the foreign table)
            n_pull = _upsert(cur, local, foreign, cols, pk, ts_col, get_wm(pull_key))
            set_wm(push_key, now)
            set_wm(pull_key, now)
            results[table] = {"push": n_push, "pull": n_pull}
            logger.info("[fdw] %s: pushed=%d pulled=%d", table, n_push, n_pull)
        primary_conn.commit()
    return results
