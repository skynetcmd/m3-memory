"""A host-less (unix-socket) DSN must not put NULL into CREATE SERVER OPTIONS.

`postgresql:///m3_mac` is the normal shape for a local cluster using peer auth:
valid, and carrying neither host nor user. `_ensure_fdw_wired` formatted those
straight into the options list, so psycopg2 rendered them as the literal NULL
and PostgreSQL rejected the statement:

    psycopg2.errors.SyntaxError: syntax error at or near "NULL"
    LINE 1: ... FOREIGN DATA WRAPPER postgres_fdw OPTIONS (host NULL, dbna...

The module already handled this for `password` ("passing password=NULL is a SQL
syntax error") and the same reasoning was simply never applied to `host` or
`user`. Found 2026-10-01 by running the macOS PG leg against the local socket
DSN; a DSN with an explicit host hides it entirely, which is why the suite had
been green on this path.

These tests drive the real function with a recording cursor, so they assert the
SQL actually emitted rather than re-deriving it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))

import pg_fdw_sync  # noqa: E402


class _RecordingCursor:
    """Minimal cursor double: records SQL, reports postgres_fdw as installed."""

    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, args=()):
        self.calls.append((sql, tuple(args) if args else ()))

    def fetchone(self):
        # Only used for the pg_extension probe; a non-None row means "installed".
        return (1,)

    def sql_for(self, fragment: str) -> list[tuple[str, tuple]]:
        return [c for c in self.calls if fragment in c[0]]


def _wire(dsn: str) -> _RecordingCursor:
    cur = _RecordingCursor()
    pg_fdw_sync._ensure_fdw_wired(cur, dsn)
    return cur


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql:///m3_mac",            # socket, no host, no user
        "postgresql:///m3_mac?x=1",        # same with a trailing param
    ],
)
def test_socket_dsn_omits_the_host_option_entirely(dsn):
    cur = _wire(dsn)
    stmts = cur.sql_for("CREATE SERVER")
    assert stmts, f"no CREATE SERVER emitted for {dsn}"
    sql, args = stmts[0]
    assert "host" not in sql, f"host option emitted for a host-less DSN: {sql}"
    assert None not in args, f"None passed as a server option: {args!r}"


def test_socket_dsn_omits_the_user_mapping_options_entirely():
    cur = _wire("postgresql:///m3_mac")
    stmts = cur.sql_for("CREATE USER MAPPING")
    assert stmts, "no CREATE USER MAPPING emitted"
    sql, args = stmts[0]
    # Neither user nor password exists, so there must be no OPTIONS clause at
    # all — `OPTIONS ()` is itself a syntax error.
    assert "OPTIONS" not in sql, f"empty OPTIONS clause emitted: {sql}"
    assert args == ()


def test_a_dsn_with_a_host_still_passes_it():
    cur = _wire("postgresql://u:p@db.example:6543/warehouse")
    sql, args = cur.sql_for("CREATE SERVER")[0]
    assert "host" in sql
    assert "db.example" in args
    assert "6543" in args
    assert "warehouse" in args

    sql, args = cur.sql_for("CREATE USER MAPPING")[0]
    assert "user" in sql and "password" in sql
    assert args == ("u", "p")


def test_a_dsn_with_a_user_but_no_password_omits_only_the_password():
    cur = _wire("postgresql://u@db.example/warehouse")
    sql, args = cur.sql_for("CREATE USER MAPPING")[0]
    assert "user" in sql
    assert "password" not in sql, f"password option emitted with no password: {sql}"
    assert args == ("u",)


def test_no_statement_ever_receives_a_none_argument():
    """The defect in one assertion: None reaching psycopg2 becomes SQL NULL."""
    for dsn in (
        "postgresql:///m3_mac",
        "postgresql://u@db.example/warehouse",
        "postgresql://u:p@db.example:6543/warehouse",
    ):
        for sql, args in _wire(dsn).calls:
            assert None not in args, f"{dsn} -> None in args for: {sql}"
