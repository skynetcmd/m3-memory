#!/usr/bin/env python3
"""One-shot migration: rename v1 entity vocabulary to v2-aligned names.

Performs in-place rename of entity_type and predicate values in any DB that
was extracted under the v1 vocabulary (default before 2026-05-03), and also
migrates 'contradicts' predicate rows that may have been extracted under
the m3 vocab (which dropped 'contradicts' alongside v2). Idempotent --
re-running has no effect on already-migrated rows since the old names no
longer exist after the first pass.

Renames performed (each affects ANY DB whose entity tables contain the
old names; the same script applies to both human-life DBs and technical
DBs since the renames are semantic substitutions):

  entities.entity_type:
    'concept'   -> 'legacy_concept'   (preserved-but-deprecated; v2-related)
    'object'    -> 'legacy_object'    (preserved-but-deprecated; v2-related)

  entity_relationships.predicate:
    'relates_to' -> 'mentions'    (v1 catch-all -> v2 catch-all)
    'contradicts' -> 'supersedes' (v1/m3 change-edge -> canonical change edge)

Left as-is (still valid in default schema as deprecated):
    'before', 'after'  predicate rows -- temporal ordering now derived from
        has_time edges, but old rows remain queryable.

Backward-compat: not maintained at the prompt level. v1 type names
('concept', 'object') and v1/m3 predicate names ('relates_to',
'contradicts') are NOT in the new default vocab; new extractors must use
the v2 / updated-m3 names. Existing rows under the old names would fail
validation on re-write until this script renames them. Reads remain fine
since validation only fires on the write path.

DBs that may need this migration: any SQLite DB with the entity_graph
tables (migration 024) that was extracted under the v1 default vocab or
the pre-2026-05-03 m3 vocab. Chatlog DBs without entity_relationships
tables are skipped at the schema-check.

Usage:
    python bin/migrate_entity_vocab.py --database <path/to/your.db>
    python bin/migrate_entity_vocab.py --database <path/to/your.db> --dry-run

The default DB resolution order (M3_DATABASE env > --database flag > default
agent_memory.db) follows the same convention as other m3-memory scripts.
"""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "bin"))
try:
    from m3_sdk import resolve_db_path  # type: ignore[import-not-found]
except ImportError:
    resolve_db_path = None

ENTITY_TYPE_RENAMES = {
    "concept": "legacy_concept",
    "object": "legacy_object",
}

PREDICATE_RENAMES = {
    "relates_to": "mentions",
    "contradicts": "supersedes",
}


def _connect(db_path: str) -> sqlite3.Connection:
    if not Path(db_path).exists():
        sys.exit(f"ERROR: database not found: {db_path}")
    return sqlite3.connect(db_path, timeout=60, isolation_level=None)


def _has_table(con: sqlite3.Connection, name: str) -> bool:
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _audit_before(con: sqlite3.Connection) -> dict:
    """Snapshot per-old-name row counts before migration."""
    counts: dict = {"entities": {}, "predicates": {}}
    if _has_table(con, "entities"):
        for old in ENTITY_TYPE_RENAMES:
            n = con.execute(
                "SELECT COUNT(*) FROM entities WHERE entity_type = ?", (old,)
            ).fetchone()[0]
            counts["entities"][old] = n
    if _has_table(con, "entity_relationships"):
        for old in PREDICATE_RENAMES:
            n = con.execute(
                "SELECT COUNT(*) FROM entity_relationships WHERE predicate = ?",
                (old,),
            ).fetchone()[0]
            counts["predicates"][old] = n
    return counts


def _audit_after(con: sqlite3.Connection) -> dict:
    """Snapshot per-new-name row counts after migration. Includes the totals
    for the destination predicate names (mentions, supersedes) so we can show
    how many of each are migrated v1 rows vs. native v2 rows."""
    counts: dict = {"entities": {}, "predicates": {}}
    if _has_table(con, "entities"):
        for new in ENTITY_TYPE_RENAMES.values():
            n = con.execute(
                "SELECT COUNT(*) FROM entities WHERE entity_type = ?", (new,)
            ).fetchone()[0]
            counts["entities"][new] = n
    if _has_table(con, "entity_relationships"):
        for new in set(PREDICATE_RENAMES.values()):
            n = con.execute(
                "SELECT COUNT(*) FROM entity_relationships WHERE predicate = ?",
                (new,),
            ).fetchone()[0]
            counts["predicates"][new] = n
    return counts


def _print_counts(label: str, counts: dict) -> None:
    print(f"  {label}:")
    if counts["entities"]:
        for k, v in counts["entities"].items():
            print(f"    entity_type {k!r}: {v:,} rows")
    if counts["predicates"]:
        for k, v in counts["predicates"].items():
            print(f"    predicate {k!r}: {v:,} rows")
    if not counts["entities"] and not counts["predicates"]:
        print("    (no relevant rows)")


def migrate(db_path: str, dry_run: bool = False) -> int:
    print(f"=== migrate_entity_vocab on {db_path} ===")
    print(f"    mode: {'DRY-RUN (no writes)' if dry_run else 'WRITE'}")
    print()

    con = _connect(db_path)
    try:
        # Schema sanity: refuse to migrate if the entity tables aren't there.
        if not _has_table(con, "entities") or not _has_table(con, "entity_relationships"):
            print("This DB does not have entity_graph tables (migration 024).")
            print("Nothing to migrate. Exiting.")
            return 0

        before = _audit_before(con)
        _print_counts("BEFORE", before)
        print()

        total_to_migrate = sum(before["entities"].values()) + sum(
            before["predicates"].values()
        )
        if total_to_migrate == 0:
            print("Already migrated (or no v1 rows). Nothing to do.")
            return 0

        if dry_run:
            print(f"DRY-RUN: would migrate {total_to_migrate:,} rows total.")
            return 0

        con.execute("BEGIN IMMEDIATE")
        for old, new in ENTITY_TYPE_RENAMES.items():
            con.execute(
                "UPDATE entities SET entity_type = ?, updated_at = "
                "strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE entity_type = ?",
                (new, old),
            )
        for old, new in PREDICATE_RENAMES.items():
            con.execute(
                "UPDATE entity_relationships SET predicate = ? WHERE predicate = ?",
                (new, old),
            )
        con.execute("COMMIT")

        after = _audit_after(con)
        print()
        _print_counts("AFTER", after)
        print()
        print(f"Migrated {total_to_migrate:,} rows.")
        return 0
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=str,
        default=os.environ.get("M3_DATABASE")
        or str(REPO_ROOT / "memory" / "agent_memory.db"),
        help="Path to the SQLite DB to migrate. Default: M3_DATABASE env "
        "or memory/agent_memory.db.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Audit row counts, print the plan, write nothing.",
    )
    args = parser.parse_args()

    db_path = (
        resolve_db_path(args.database) if resolve_db_path else os.path.abspath(args.database)
    )
    return migrate(db_path, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
