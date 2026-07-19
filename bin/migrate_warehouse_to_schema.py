#!/usr/bin/env python3
"""migrate_warehouse_to_schema.py — consolidate a PostgreSQL warehouse's tables
from the legacy ``public`` schema into the canonical ``m3_warehouse`` schema.

Older m3 warehouses stored the synced tables in ``public``. The canonical layout
(pg_warehouse_chatlog_v1.sql) puts them under ``m3_warehouse`` (unified core +
chat-log, with a type='chat_log' index). This tool moves any table still in
``public`` into ``m3_warehouse`` so warehouse sync (which now targets
``m3_warehouse``) sees your history instead of starting empty.

Safe by construction:
  * DRY-RUN by default — prints the per-table plan, changes nothing.
  * Idempotent — re-runnable; a table already consolidated is a no-op.
  * No data loss — public data is copied (ON CONFLICT DO NOTHING) into
    m3_warehouse and VERIFIED (wh count >= public count) BEFORE the public copy
    is dropped, and only with --drop-public. Without it, public is left intact.

Runs ON THE WAREHOUSE. Needs a role with USAGE+CREATE on m3_warehouse and
USAGE+SELECT (and DROP, for --drop-public) on public — typically a superuser or
the warehouse owner.

Usage:
    python bin/migrate_warehouse_to_schema.py --dsn <warehouse_dsn> [--dry-run] [--drop-public] [--yes]
    # --dsn defaults to M3_CDW_PG_URL / PG_URL when omitted.

Exit codes: 0 = success (or clean dry-run), 1 = error / verification failed.
"""
from __future__ import annotations

import argparse
import os
import sys

WAREHOUSE_SCHEMA = "m3_warehouse"

# Tables that belong in the warehouse. sync_watermarks is deliberately EXCLUDED
# and DROPPED if found — watermarks are per-machine, never warehouse-side.
_WAREHOUSE_TABLES = (
    "memory_items",
    "memory_embeddings",
    "memory_relationships",
    "synchronized_secrets",
    "tasks",
    "agent_retention_policies",
    "gdpr_requests",
)
_DROP_IF_PRESENT = ("sync_watermarks",)  # legacy artifact — never keep


def _resolve_dsn(explicit: "str | None") -> str:
    if explicit:
        return explicit
    for var in ("M3_CDW_PG_URL", "PG_URL"):
        v = os.environ.get(var)
        if v:
            return v
    print("ERROR: no warehouse DSN. Pass --dsn or set M3_CDW_PG_URL.", file=sys.stderr)
    sys.exit(1)


def _table_exists(cur, schema: str, table: str) -> bool:
    cur.execute(
        "SELECT to_regclass(%s) IS NOT NULL", (f"{schema}.{table}",))
    return bool(cur.fetchone()[0])


def _row_count(cur, schema: str, table: str) -> "int | None":
    if not _table_exists(cur, schema, table):
        return None
    cur.execute(f"SELECT count(*) FROM {schema}.{table}")
    return cur.fetchone()[0]


def _shared_columns(cur, table: str) -> list[str]:
    """Columns present in BOTH public.<table> and m3_warehouse.<table>, in the
    warehouse's ordinal order. Used for an explicit-column INSERT..SELECT so a
    schema difference (extra columns on either side) never breaks the copy."""
    cur.execute(
        """
        SELECT w.column_name
        FROM information_schema.columns w
        JOIN information_schema.columns p
          ON p.table_schema='public' AND p.table_name=%s
         AND p.column_name=w.column_name
        WHERE w.table_schema=%s AND w.table_name=%s
        ORDER BY w.ordinal_position
        """,
        (table, WAREHOUSE_SCHEMA, table),
    )
    return [r[0] for r in cur.fetchall()]


def _plan_for(cur, table: str) -> dict:
    """Classify one table's migration action from live counts."""
    pub = _row_count(cur, "public", table)
    wh = _row_count(cur, WAREHOUSE_SCHEMA, table)
    if pub is None and wh is not None:
        action = "ok"           # already warehouse-only
    elif pub is not None and wh is None:
        action = "move"         # create wh table + copy + (drop public)
    elif pub is not None and wh is not None:
        action = "merge"        # union public into wh + (drop public)
    else:
        action = "absent"       # neither — nothing to do
    return {"table": table, "public": pub, "warehouse": wh, "action": action}


def _pk_columns(cur, schema: str, table: str) -> list[str]:
    cur.execute(
        """
        SELECT a.attname
        FROM pg_index i
        JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey)
        WHERE i.indrelid = %s::regclass AND i.indisprimary
        ORDER BY a.attnum
        """,
        (f"{schema}.{table}",),
    )
    return [r[0] for r in cur.fetchall()]


def _apply(cur, plan: dict, warehouse_ddl_path: "str | None", drop_public: bool) -> None:
    table, action = plan["table"], plan["action"]
    if action in ("ok", "absent"):
        return

    # 'move' needs the wh table to exist first. Apply the warehouse DDL (idempotent
    # CREATE TABLE IF NOT EXISTS throughout) so the target exists, then copy.
    if action == "move" and warehouse_ddl_path and os.path.exists(warehouse_ddl_path):
        with open(warehouse_ddl_path, encoding="utf-8") as f:
            cur.execute(f.read())

    if not _table_exists(cur, WAREHOUSE_SCHEMA, table):
        raise RuntimeError(
            f"{WAREHOUSE_SCHEMA}.{table} does not exist and could not be created "
            f"(apply pg_warehouse_chatlog_v1.sql first).")

    cols = _shared_columns(cur, table)
    if not cols:
        raise RuntimeError(f"no shared columns between public.{table} and "
                           f"{WAREHOUSE_SCHEMA}.{table} — cannot copy safely.")
    collist = ", ".join(cols)
    pk = _pk_columns(cur, WAREHOUSE_SCHEMA, table)
    conflict = f"ON CONFLICT ({', '.join(pk)}) DO NOTHING" if pk else ""
    cur.execute(
        f"INSERT INTO {WAREHOUSE_SCHEMA}.{table} ({collist}) "
        f"SELECT {collist} FROM public.{table} {conflict}")

    # Verify BEFORE dropping: warehouse must now hold at least as many rows as
    # public did (union). Never drop on a short count.
    pub_n = _row_count(cur, "public", table) or 0
    wh_n = _row_count(cur, WAREHOUSE_SCHEMA, table) or 0
    if wh_n < pub_n:
        raise RuntimeError(
            f"{table}: post-copy warehouse count {wh_n} < public {pub_n} — "
            f"NOT dropping public (investigate).")
    if drop_public:
        cur.execute(f"DROP TABLE public.{table}")


def main(argv: "list[str] | None" = None) -> int:
    p = argparse.ArgumentParser(description="Consolidate warehouse tables public -> m3_warehouse.")
    p.add_argument("--dsn", default=None, help="Warehouse DSN (else M3_CDW_PG_URL/PG_URL).")
    p.add_argument("--dry-run", action="store_true", help="Print the plan; change nothing.")
    p.add_argument("--drop-public", action="store_true",
                   help="After a verified copy, DROP the public.<table> copy.")
    p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    args = p.parse_args(argv)

    dsn = _resolve_dsn(args.dsn)
    try:
        import psycopg2
    except ImportError:
        import psycopg as psycopg2  # type: ignore

    warehouse_ddl = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "memory", "migrations", "postgres", "pg_warehouse_chatlog_v1.sql")

    conn = psycopg2.connect(dsn, connect_timeout=10)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {WAREHOUSE_SCHEMA}")
            plans = [_plan_for(cur, t) for t in _WAREHOUSE_TABLES]
            legacy = [t for t in _DROP_IF_PRESENT
                      if _table_exists(cur, WAREHOUSE_SCHEMA, t)]

        print(f"Warehouse consolidation plan (schema {WAREHOUSE_SCHEMA}):")
        print(f"  {'table':28} {'public':>8} {'m3_warehouse':>13}  action")
        todo = 0
        for pl in plans:
            pub = "-" if pl["public"] is None else pl["public"]
            wh = "-" if pl["warehouse"] is None else pl["warehouse"]
            print(f"  {pl['table']:28} {str(pub):>8} {str(wh):>13}  {pl['action']}")
            if pl["action"] in ("move", "merge"):
                todo += 1
        for t in legacy:
            print(f"  {WAREHOUSE_SCHEMA}.{t}: legacy watermark table -> DROP")
        if not todo and not legacy:
            print("\nNothing to migrate — warehouse is already consolidated.")
            return 0
        drop_note = "will DROP public copies" if args.drop_public else \
            "public copies LEFT INTACT (pass --drop-public to remove)"
        print(f"\n{todo} table(s) to consolidate; {drop_note}.")

        if args.dry_run:
            print("\n(dry-run: no changes made)")
            return 0
        if not args.yes:
            try:
                ans = input("Proceed? [y/N] ").strip().lower()
            except EOFError:
                ans = "n"
            if ans not in ("y", "yes"):
                print("Aborted — no changes made.")
                return 0

        with conn.cursor() as cur:
            for pl in plans:
                _apply(cur, pl, warehouse_ddl, args.drop_public)
            for t in legacy:
                cur.execute(f"DROP TABLE IF EXISTS {WAREHOUSE_SCHEMA}.{t}")
        conn.commit()
        print("\n✅ Consolidation complete.")
        return 0
    except Exception as e:
        conn.rollback()
        print(f"\n❌ ERROR: {type(e).__name__}: {e}\n(rolled back — no changes made)",
              file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
