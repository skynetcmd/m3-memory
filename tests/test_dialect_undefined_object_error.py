"""dialect().is_undefined_object_error() — portable "missing column/table" classifier.

Pre-migration-tolerant code paths (memory_maintenance's decay/reinforce/purge)
must catch "no such column/table" and degrade to a no-op, but NOT swallow real
errors. The two backends signal this differently — SQLite via an OperationalError
message, PostgreSQL via SQLSTATE 42703/42P01 — so callers ask the seam, not the
message. The PG-live half is covered in test_memory_maintenance_pg_live; this pins
the SQLite classifier + the false-positive guard.
"""
import os
import sqlite3
import sys

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from memory.backends.sqlite_backend import SqliteDialect  # noqa: E402


def _raise(sql, setup=None):
    c = sqlite3.connect(":memory:")
    if setup:
        c.executescript(setup)
    try:
        c.execute(sql)
    except Exception as e:  # noqa: BLE001
        return e
    raise AssertionError(f"expected {sql!r} to raise")


def test_missing_column_is_classified():
    d = SqliteDialect()
    e = _raise("SELECT nope FROM t", setup="CREATE TABLE t(id TEXT)")
    assert d.is_undefined_object_error(e) is True


def test_missing_table_is_classified():
    d = SqliteDialect()
    e = _raise("SELECT * FROM nope_table")
    assert d.is_undefined_object_error(e) is True


def test_syntax_error_is_NOT_classified():
    """The tolerance must not swallow a genuine SQL error."""
    d = SqliteDialect()
    e = _raise("SELCT bad syntax")
    assert d.is_undefined_object_error(e) is False


def test_unrelated_error_is_not_classified():
    d = SqliteDialect()
    assert d.is_undefined_object_error(ValueError("something else")) is False


def test_chained_cause_is_inspected():
    """A wrapped error still classifies via __cause__."""
    d = SqliteDialect()
    inner = _raise("SELECT * FROM nope_table")
    outer = RuntimeError("seam wrapper")
    outer.__cause__ = inner
    assert d.is_undefined_object_error(outer) is True
