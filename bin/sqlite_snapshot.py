"""sqlite_snapshot.py — the one WAL-safe, verified copy of a SQLite file.

WHY: In WAL mode, committed transactions live in ``<db>-wal`` until a checkpoint
folds them into the main file. ``shutil.copy2(db_path, ...)`` copies only the
main file, so a "backup" taken that way silently omits recent writes — and a
copy taken mid-write can be torn (DESIGN_PHILOSOPHIES §10). Five tools did
exactly that before their destructive step. This module is the single owner of
the correct procedure, so no caller decides it for itself again (§10a).

HOW:
  1. Open the source read-only and BEGIN a read transaction. In WAL mode that
     pins one snapshot of the database.
  2. Count every table's rows inside that transaction.
  3. Run the online backup API on the SAME connection. It copies the pinned
     snapshot — measured: rows committed by another writer after step 1 are
     absent from the copy, so the counts from step 2 describe it exactly and a
     concurrent writer cannot cause a false mismatch.
  4. Convert the copy to ``journal_mode=DELETE`` so it is one self-contained
     file with no ``-wal`` of its own.
  5. Verify the copy: ``PRAGMA quick_check`` must say ``ok`` and every table's
     row count must equal step 2. Only then is it renamed from ``.partial`` to
     its final name. Any failure removes the partial and raises.

Stdlib only, so bootstrap and migration tools (``migrate_memory``,
``homecoming``) can use it without importing the ``memory`` package. Callers
inside the storage seam reach it through ``SqliteBackend.snapshot``.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Optional


class SqliteSnapshotError(RuntimeError):
    """The copy could not be taken or did not verify. Nothing was left behind."""


def user_tables(conn: sqlite3.Connection) -> "list[str]":
    """Real tables to count: excludes SQLite internals and VIRTUAL tables.

    A virtual table (FTS5) is a view over its shadow tables, which ARE real
    tables and are counted; counting the virtual table itself would be a full
    index scan that verifies nothing the shadow tables do not.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite_%' "
        "AND COALESCE(sql, '') NOT LIKE 'CREATE VIRTUAL TABLE%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def row_counts(conn: sqlite3.Connection, tables: "list[str]") -> "dict[str, int]":
    out: dict[str, int] = {}
    for t in tables:
        # Identifier comes from sqlite_master and is quoted; no user input.
        out[t] = conn.execute(f"SELECT COUNT(*) FROM {_quote_ident(t)}").fetchone()[0]  # nosec B608
    return out


def _ro_uri(path: str) -> str:
    # as_uri() percent-encodes spaces, '?', '#' and '%', which a hand-built
    # "file:{path}?mode=ro" would let SQLite misparse as URI syntax.
    return Path(path).resolve().as_uri() + "?mode=ro"


def _remove_quietly(path: str) -> None:
    for p in (path, path + "-wal", path + "-shm", path + "-journal"):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def snapshot_sqlite(
    src: str,
    dst: str,
    *,
    src_conn: Optional[sqlite3.Connection] = None,
    timeout: float = 60.0,
) -> "dict[str, int]":
    """Copy ``src`` to ``dst`` WAL-safely and verify it. Returns the row counts.

    ``src_conn``: copy through a connection the caller already holds (a
    migration snapshotting the database it has open). It must have no
    uncommitted writes — the backup would copy them. An open read transaction
    is reused as the snapshot; otherwise one is opened and closed here.

    Raises :class:`SqliteSnapshotError` if ``dst`` exists, the source is
    missing or unreadable, or the copy does not verify.
    """
    if os.path.exists(dst):
        raise SqliteSnapshotError(f"refusing to overwrite an existing file: {dst}")
    if src_conn is None and not os.path.isfile(src):
        raise SqliteSnapshotError(f"source database does not exist: {src}")
    partial = dst + ".partial"
    _remove_quietly(partial)
    parent = os.path.dirname(os.path.abspath(dst))
    os.makedirs(parent, exist_ok=True)

    own_conn = src_conn is None
    try:
        conn = src_conn if src_conn is not None else sqlite3.connect(
            _ro_uri(src), uri=True, timeout=timeout)
    except sqlite3.Error as e:
        raise SqliteSnapshotError(f"cannot open {src} read-only: {e}") from e

    opened_txn = False
    try:
        try:
            if not conn.in_transaction:
                conn.execute("BEGIN")
                opened_txn = True
            tables = user_tables(conn)
            expected = row_counts(conn, tables)
            out = sqlite3.connect(partial)
            try:
                conn.backup(out)
                out.execute("PRAGMA journal_mode=DELETE")
            finally:
                out.close()
        finally:
            if opened_txn:
                conn.rollback()
            if own_conn:
                conn.close()
    except sqlite3.Error as e:
        _remove_quietly(partial)
        raise SqliteSnapshotError(f"backup of {src} failed: {e}") from e

    try:
        check = sqlite3.connect(_ro_uri(partial), uri=True)
        try:
            qc = check.execute("PRAGMA quick_check").fetchone()[0]
            got = row_counts(check, user_tables(check))
        finally:
            check.close()
    except sqlite3.Error as e:
        _remove_quietly(partial)
        raise SqliteSnapshotError(f"copy of {src} is unreadable: {e}") from e

    if qc != "ok" or got != expected:
        _remove_quietly(partial)
        diffs = sorted(t for t in set(expected) | set(got) if expected.get(t) != got.get(t))
        raise SqliteSnapshotError(
            f"copy of {src} did not verify: quick_check={qc!r}; "
            f"row-count mismatch in {diffs[:10] or 'none'}"
        )
    os.replace(partial, dst)
    return expected
