"""SQL dialect helpers — the one place backend SQL divergences are decided.

The mechanical port (plan §6) scatters dozens of tiny SQLite-isms across the
codebase: ``?`` placeholders, ``INSERT OR IGNORE``, ``json_extract(col,'$.k')``,
``strftime('now')``, ``AUTOINCREMENT``. Rather than sprinkle ``if backend ==
"postgres"`` at every call site, each divergence gets ONE helper here. A caller
asks the dialect for the fragment it needs; the dialect knows the target.

This is deliberately NOT a query builder or an ORM (that is the fat-abstraction
failure mode DESIGN_PHILOSOPHIES §1 warns against). It is a flat bag of small,
pure string helpers — each maps a single known SQLite construct to its Postgres
equivalent, nothing more. Everything is a pure function of ``name`` + args, so it
is fully unit-testable with no database (Phase 1a).

Parameterized SQL only (§6 of the philosophies): these helpers emit *placeholders
and structural SQL*, never interpolated user values. ``json_path`` and column
names are trusted, caller-supplied identifiers, never end-user input.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .base import BackendName

ParamStyle = Literal["qmark", "format"]  # sqlite '?'  vs  psycopg '%s'


@dataclass(frozen=True)
class Dialect:
    """A backend's SQL surface. Small, pure, table-free — safe to unit test.

    Obtain via :func:`dialect_for`; do not construct per call site. Two frozen
    singletons (SQLITE, POSTGRES) are shared.
    """

    backend: BackendName
    param_style: ParamStyle

    # -- bind placeholders ---------------------------------------------------
    def placeholder(self, n: int = 1) -> str:
        """Render ``n`` positional bind placeholders, comma-joined.

        ``placeholder(3)`` → ``"?, ?, ?"`` (sqlite) or ``"%s, %s, %s"`` (pg).
        Replaces the scattered ``",".join("?" * n)`` idiom. ``n`` must be >= 1.
        """
        if n < 1:
            raise ValueError(f"placeholder count must be >= 1, got {n}")
        token = "?" if self.param_style == "qmark" else "%s"
        return ", ".join([token] * n)

    def param(self) -> str:
        """A single bind placeholder (``?`` / ``%s``)."""
        return "?" if self.param_style == "qmark" else "%s"

    # -- upsert --------------------------------------------------------------
    def insert_or_ignore(self) -> str:
        """The INSERT verb+suffix for "insert, skip on PK/unique conflict".

        SQLite: ``INSERT OR IGNORE INTO`` (prefix form).
        Postgres has no verb form — the arbiter goes in a trailing clause, so
        this returns plain ``INSERT INTO`` and the caller appends
        :meth:`on_conflict_ignore`. Kept as one helper pair so a Tier-1 pass can
        translate ``INSERT OR IGNORE INTO t ...`` → ``INSERT INTO t ... {suffix}``
        mechanically.
        """
        return "INSERT OR IGNORE INTO" if self.backend == "sqlite" else "INSERT INTO"

    def on_conflict_ignore(self, *, conflict_target: str = "") -> str:
        """Trailing clause that makes an INSERT a no-op on conflict.

        SQLite: empty (the OR IGNORE prefix already did it).
        Postgres: ``ON CONFLICT [(cols)] DO NOTHING``. ``conflict_target`` is an
        optional parenthesized column list or constraint, e.g. ``"(id)"``.
        """
        if self.backend == "sqlite":
            return ""
        tgt = f" {conflict_target}" if conflict_target else ""
        return f"ON CONFLICT{tgt} DO NOTHING"

    def on_conflict_update(self, conflict_target: str, set_columns: list[str]) -> str:
        """Trailing UPSERT clause: on conflict, overwrite the given columns.

        Both backends spell this ``ON CONFLICT (...) DO UPDATE SET c = excluded.c``
        — the ``excluded`` pseudo-table name is shared. ``conflict_target`` is the
        parenthesized arbiter, e.g. ``"(id)"``; ``set_columns`` are overwritten
        from the would-be-inserted row.
        """
        if not set_columns:
            raise ValueError("on_conflict_update needs at least one column")
        assigns = ", ".join(f"{c} = excluded.{c}" for c in set_columns)
        return f"ON CONFLICT {conflict_target} DO UPDATE SET {assigns}"

    # -- time ----------------------------------------------------------------
    def now(self) -> str:
        """SQL expression for "current timestamp", for use inside statements.

        SQLite stores ISO-8601 text; Postgres uses ``NOW()`` (TIMESTAMPTZ). The
        SQLite form matches the existing column default exactly so DDL generated
        for either backend keeps identical semantics.
        """
        if self.backend == "sqlite":
            return "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        return "NOW()"

    # -- JSON ----------------------------------------------------------------
    def json_extract_text(self, column: str, json_path: str) -> str:
        """Extract a top-level JSON field AS TEXT.

        ``json_extract_text("metadata_json", "provider")`` →
        SQLite: ``json_extract(metadata_json, '$.provider')``
        Postgres: ``metadata_json ->> 'provider'``

        WARNING (plan §6 Tier-2): on Postgres, ``metadata_json`` is JSONB, so a
        *whole-column* SELECT returns a dict from psycopg — Python-side
        ``json.loads()`` on it raises ``TypeError``. This helper only covers the
        SQL-side single-field extraction; the whole-column read must be guarded
        separately. ``json_path`` is a trusted key name, never end-user input.
        """
        # The key is interpolated into a quoted SQL literal on both backends, so
        # it must be a bare identifier (no quote, dot, bracket, semicolon).
        if not json_path.isidentifier():
            raise ValueError(f"json_path must be a bare identifier: {json_path!r}")
        if self.backend == "sqlite":
            return f"json_extract({column}, '$.{json_path}')"
        return f"{column} ->> '{json_path}'"

    # -- introspection -------------------------------------------------------
    def columns_of(self, table: str) -> tuple[str, tuple]:
        """A (sql, params) pair listing a table's column names.

        Replaces ``PRAGMA table_info(t)`` (SQLite) with an
        ``information_schema.columns`` query (Postgres). Returns column names in
        ordinal position. ``table`` is a trusted identifier.
        """
        # PRAGMA interpolates the table name (can't be bound), so the name must
        # be a bare SQL identifier. Reject anything else on BOTH backends so the
        # two paths accept exactly the same inputs (parity), even though the pg
        # path binds it as a parameter and would be injection-safe regardless.
        if not table.isidentifier():
            raise ValueError(f"table name must be a bare identifier: {table!r}")
        if self.backend == "sqlite":
            # PRAGMA can't be parameterized; table is trusted. Caller reads row[1].
            return (f"PRAGMA table_info({table})", ())
        sql = (
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s ORDER BY ordinal_position"
        )
        return (sql, (table,))


SQLITE = Dialect(backend="sqlite", param_style="qmark")
POSTGRES = Dialect(backend="postgres", param_style="format")

_BY_NAME = {"sqlite": SQLITE, "postgres": POSTGRES}


def dialect_for(backend: BackendName) -> Dialect:
    """Return the shared :class:`Dialect` singleton for a backend name."""
    try:
        return _BY_NAME[backend]
    except KeyError:
        raise ValueError(f"no dialect for backend {backend!r}") from None
