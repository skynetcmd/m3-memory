"""The pre-write snapshot primitive: verified, WAL-safe, and the same on both backends.

Five tools used to "back up" with ``shutil.copy2(db_path, ...)`` before deleting
or rewriting rows. In WAL mode that omits every commit still in ``<db>-wal``;
on PostgreSQL it copied an unrelated local SQLite file and called it a
rollback. These tests pin the replacement's guarantees, each with a case that
would have failed under the old copy.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import uuid

import pytest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import sqlite_snapshot  # noqa: E402
from sqlite_snapshot import SqliteSnapshotError, snapshot_sqlite  # noqa: E402


def _wal_store(path, rows=500):
    """A WAL store whose rows are committed but NOT checkpointed into the file."""
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA wal_autocheckpoint=0")
    c.execute("CREATE TABLE t(x)")
    c.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(rows)])
    c.commit()
    return c  # keep open: closing the last connection would checkpoint


def _count(path, table="t"):
    c = sqlite3.connect(path)
    try:
        return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        c.close()


# ── SQLite ──────────────────────────────────────────────────────────────────

def test_rows_still_in_the_wal_are_in_the_snapshot(tmp_path):
    src = str(tmp_path / "a.db")
    holder = _wal_store(src)
    try:
        assert os.path.getsize(src + "-wal") > 0, "precondition: rows live in the WAL"
        # What the old shutil.copy2 produced: the main file alone, without the rows.
        import shutil
        plain = tmp_path / "plain.db"
        shutil.copy2(src, plain)
        assert not _has_table(plain) or _count(str(plain)) == 0, "precondition: a file copy loses them"

        counts = snapshot_sqlite(src, str(tmp_path / "snap.db"))
    finally:
        holder.close()
    assert counts == {"t": 500}
    assert _count(str(tmp_path / "snap.db")) == 500


def _has_table(path):
    c = sqlite3.connect(path)
    try:
        return c.execute("SELECT 1 FROM sqlite_master WHERE name='t'").fetchone() is not None
    finally:
        c.close()


def test_snapshot_is_one_self_contained_file(tmp_path):
    src = str(tmp_path / "a.db")
    holder = _wal_store(src, rows=10)
    try:
        snapshot_sqlite(src, str(tmp_path / "snap.db"))
    finally:
        holder.close()
    c = sqlite3.connect(tmp_path / "snap.db")
    try:
        assert c.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        c.close()
    left = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("snap"))
    assert left == ["snap.db"], f"no -wal/-shm/.partial may be left behind: {left}"


def test_a_concurrent_writer_cannot_cause_a_false_mismatch(tmp_path):
    """Counts and copy come from ONE read snapshot, so commits that land
    between them are neither copied nor counted. Counting outside the
    snapshot would report a mismatch on a perfectly good copy."""
    src = str(tmp_path / "a.db")
    writer = _wal_store(src, rows=100)
    reader = sqlite3.connect(src, isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT COUNT(*) FROM t").fetchone()
        writer.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(50)])
        writer.commit()
        counts = snapshot_sqlite(src, str(tmp_path / "snap.db"), src_conn=reader)
    finally:
        reader.close()
        writer.close()
    assert counts == {"t": 100}
    assert _count(str(tmp_path / "snap.db")) == 100


def test_a_copy_that_does_not_verify_raises_and_leaves_nothing(tmp_path, monkeypatch):
    """Negative control: the verification must be able to fail."""
    src = str(tmp_path / "a.db")
    holder = _wal_store(src, rows=5)
    real = sqlite_snapshot.row_counts
    calls = {"n": 0}

    def lying_counts(conn, tables):
        calls["n"] += 1
        got = real(conn, tables)
        return got if calls["n"] == 1 else {k: v + 1 for k, v in got.items()}

    monkeypatch.setattr(sqlite_snapshot, "row_counts", lying_counts)
    try:
        with pytest.raises(SqliteSnapshotError, match="did not verify"):
            snapshot_sqlite(src, str(tmp_path / "snap.db"))
    finally:
        holder.close()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("snap")]


def test_refuses_to_overwrite_and_never_creates_a_missing_source(tmp_path):
    (tmp_path / "exists.db").write_bytes(b"x")
    src = str(tmp_path / "a.db")
    _wal_store(src, rows=1).close()
    with pytest.raises(SqliteSnapshotError, match="overwrite"):
        snapshot_sqlite(src, str(tmp_path / "exists.db"))
    missing = str(tmp_path / "missing.db")
    with pytest.raises(SqliteSnapshotError, match="does not exist"):
        snapshot_sqlite(missing, str(tmp_path / "out.db"))
    assert not os.path.exists(missing), "a snapshot must not create the store it reads"


def test_label_cannot_escape_the_backup_dir(tmp_path):
    from memory.backends.base import SnapshotError, snapshot_target

    for bad in ("../x", "a/b", "a\\b", "", "x y"):
        with pytest.raises(SnapshotError):
            snapshot_target(str(tmp_path), "agent_memory", bad, ".db")


# ── the caller helper ───────────────────────────────────────────────────────

def test_snapshot_stores_copies_each_distinct_store_once(tmp_path, monkeypatch):
    monkeypatch.delenv("M3_DB_BACKEND", raising=False)
    from memory.backends import selector

    selector._reset_for_tests()
    from m3_core.paths import snapshot_stores

    a, b = str(tmp_path / "agent_memory.db"), str(tmp_path / "agent_chatlog.db")
    ha, hb = _wal_store(a, rows=3), _wal_store(b, rows=4)
    try:
        snaps = snapshot_stores([a, b, a], tmp_path / "bk", label="pre-test")
    finally:
        ha.close()
        hb.close()
    assert [s.row_counts for s in snaps] == [{"t": 3}, {"t": 4}]
    assert all(os.path.isfile(s.path) for s in snaps)
    assert all(os.path.dirname(s.path) == str(tmp_path / "bk") for s in snaps)


def test_snapshot_stores_raises_rather_than_returning_an_unverified_copy(tmp_path, monkeypatch):
    monkeypatch.delenv("M3_DB_BACKEND", raising=False)
    from memory.backends import selector
    from memory.backends.base import SnapshotError

    selector._reset_for_tests()
    from m3_core.paths import snapshot_stores

    with pytest.raises(SnapshotError):
        snapshot_stores([str(tmp_path / "nope.db")], tmp_path / "bk", label="pre-test")


# ── migration callers ───────────────────────────────────────────────────────

def test_homecoming_copy_failure_raises_instead_of_logging(tmp_path):
    import homecoming

    with pytest.raises(SqliteSnapshotError):
        homecoming.backup_db(str(tmp_path / "missing.db"), str(tmp_path / "new" / "x.db"))


def test_migrate_backup_has_no_torn_copy_fallback(tmp_path, monkeypatch):
    import migrate_memory

    src = str(tmp_path / "agent_memory.db")
    _wal_store(src, rows=2).close()

    def boom(*a, **k):
        raise SqliteSnapshotError("simulated")

    monkeypatch.setattr(migrate_memory, "snapshot_sqlite", boom)
    target = migrate_memory.MigrationTarget(name="main", db_path=src, migrations_dir=str(tmp_path))
    with pytest.raises(SqliteSnapshotError):
        migrate_memory.take_backup(str(tmp_path / "bk"), 1, "pre-up", target)
    bk = tmp_path / "bk"
    assert not bk.exists() or not any(bk.rglob("*.db")), "no fallback file copy may be written"


# ── PostgreSQL (live) ───────────────────────────────────────────────────────

def _pg_tools_present():
    import shutil
    return bool(shutil.which("pg_dump") and shutil.which("pg_restore"))


@pytest.fixture()
def pg_schema_backend():
    from conftest import pg_dsn

    dsn = pg_dsn()
    if not dsn:
        pytest.skip("no PostgreSQL DSN")
    if not _pg_tools_present():
        pytest.skip("pg_dump/pg_restore not installed")
    import psycopg2

    schema = "snap_" + uuid.uuid4().hex[:10]
    admin = psycopg2.connect(dsn)
    admin.autocommit = True
    admin.cursor().execute(f'CREATE SCHEMA "{schema}"')
    sep = "&" if "?" in dsn else "?"
    scoped = f"{dsn}{sep}options=-csearch_path%3D{schema}"
    from memory.backends.postgres_backend import PostgresBackend

    b = PostgresBackend(dsn=scoped)
    with b.connection() as c:
        cur = c.cursor()
        cur.execute("CREATE TABLE items (id int, body text)")
        cur.execute("CREATE TABLE empty_one (id int)")
        cur.execute(
            "INSERT INTO items SELECT g, 'line one' || chr(10) || 'line two ' || g "
            "FROM generate_series(1, 300) g"
        )
    try:
        yield b, schema, scoped
    finally:
        b.close()
        admin.cursor().execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


@pytest.mark.requires_pg
def test_pg_snapshot_verifies_and_survives_a_concurrent_writer(pg_schema_backend, tmp_path, monkeypatch):
    """Rows committed after the snapshot export are in neither the dump nor
    the counts, and multi-line values still count as one row each."""
    b, schema, _ = pg_schema_backend
    real = type(b)._run_pg_dump
    seen = {}

    def write_then_dump(self, argv, env):
        seen["argv"], seen["env"] = argv, env
        with self.connection() as c:
            c.cursor().execute("INSERT INTO items SELECT g, 'late' FROM generate_series(1, 25) g")
        return real(self, argv, env)

    monkeypatch.setattr(type(b), "_run_pg_dump", write_then_dump)
    snap = b.snapshot(str(tmp_path), label="pre-test")
    assert snap.row_counts == {"items": 300, "empty_one": 0}
    assert os.path.isfile(snap.path) and snap.path.endswith(".dump")
    assert not os.path.exists(snap.path + ".partial")
    with b.connection() as c:
        cur = c.cursor()
        cur.execute("SELECT COUNT(*) FROM items")
        assert cur.fetchone()[0] == 325, "the live store kept the late rows"


@pytest.mark.requires_pg
def test_pg_password_never_reaches_the_command_line(pg_schema_backend, tmp_path, monkeypatch):
    from psycopg2.extensions import parse_dsn

    b, _, scoped = pg_schema_backend
    password = parse_dsn(scoped).get("password")
    if not password:
        pytest.skip("DSN has no password to leak")
    real = type(b)._run_pg_dump
    seen = {}

    def spy(self, argv, env):
        seen["argv"], seen["env"] = argv, env
        return real(self, argv, env)

    monkeypatch.setattr(type(b), "_run_pg_dump", spy)
    b.snapshot(str(tmp_path), label="pre-test")
    assert not any(password in a for a in seen["argv"])
    assert seen["env"].get("PGPASSWORD") == password


@pytest.mark.requires_pg
def test_pg_truncated_archive_fails_verification(pg_schema_backend, tmp_path):
    """Negative control: reading the archive back must be able to fail."""
    from memory.backends.base import SnapshotError
    from memory.backends.postgres_backend import _archive_row_counts, _pg_tool

    b, schema, _ = pg_schema_backend
    snap = b.snapshot(str(tmp_path), label="pre-test")
    with open(snap.path, "rb") as f:
        data = f.read()
    cut = tmp_path / "cut.dump"
    cut.write_bytes(data[: len(data) // 2])
    with pytest.raises(SnapshotError):
        _archive_row_counts(_pg_tool("pg_restore"), str(cut), schema)


@pytest.mark.requires_pg
def test_pg_core_and_chatlog_targets_are_one_store(pg_schema_backend):
    b, _, _ = pg_schema_backend
    assert b.store_identity().startswith("postgres:")
    assert "@" not in b.store_identity() and ":" + "//" not in b.store_identity()


def test_missing_pg_tools_refuse_with_instructions(monkeypatch):
    from memory.backends.base import SnapshotError
    from memory.backends.postgres_backend import _pg_tool

    monkeypatch.delenv("M3_PG_DUMP", raising=False)
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    with pytest.raises(SnapshotError, match="Refusing to continue without a backup"):
        _pg_tool("pg_dump")
