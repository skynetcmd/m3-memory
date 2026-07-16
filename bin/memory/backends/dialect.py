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

    def on_conflict_ignore(
        self, *, conflict_target: str = "", index_predicate: str = ""
    ) -> str:
        """Trailing clause that makes an INSERT a no-op on conflict.

        SQLite: empty (the OR IGNORE prefix already did it).
        Postgres: ``ON CONFLICT [(cols) [WHERE pred]] DO NOTHING``.

        ``conflict_target`` is the parenthesized arbiter, e.g. ``"(id)"`` or
        ``"(memory_id, source_kind, source_ref)"``. ``index_predicate`` is the
        WHERE clause of a PARTIAL unique index (e.g. ``"delta > 0"``) — Postgres
        requires it in the arbiter to match a partial index, or the conflict is
        not caught. Both are trusted, caller-supplied SQL fragments.

        Note: with no ``conflict_target``, Postgres ``DO NOTHING`` catches a
        conflict on ANY unique/exclusion constraint — which is what a target-less
        SQLite ``OR IGNORE`` does. Prefer naming the target when a specific
        (possibly partial) index carries the dedup semantics.
        """
        if self.backend == "sqlite":
            return ""
        if index_predicate and not conflict_target:
            raise ValueError(
                "index_predicate requires a conflict_target (partial-index arbiter)"
            )
        tgt = f" {conflict_target}" if conflict_target else ""
        pred = f" WHERE {index_predicate}" if index_predicate else ""
        return f"ON CONFLICT{tgt}{pred} DO NOTHING"

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

    def json_extract_int(self, column: str, json_path: str) -> str:
        """Extract a top-level JSON field AS INTEGER (a numeric-cast expression).

        The graph session-window queries read ``session_idx`` / ``turn_idx`` out
        of ``metadata_json`` and compare/order them numerically. In SQLite that is
        ``CAST(json_extract(col, '$.k') AS INTEGER)``; in Postgres JSONB it is
        ``(col ->> 'k')::int`` (``->>`` yields text, cast to int).

        ``json_extract_int("metadata_json", "session_idx")`` →
        SQLite:   ``CAST(json_extract(metadata_json, '$.session_idx') AS INTEGER)``
        Postgres: ``(metadata_json ->> 'session_idx')::int``

        A missing/NULL key yields SQL NULL on both backends (json_extract →
        NULL → CAST NULL; ``->>`` missing → NULL → ``::int`` NULL), so ``IS NULL``
        checks behave identically. ``json_path`` is a trusted bare identifier.
        """
        if not json_path.isidentifier():
            raise ValueError(f"json_path must be a bare identifier: {json_path!r}")
        if self.backend == "sqlite":
            return f"CAST(json_extract({column}, '$.{json_path}') AS INTEGER)"
        return f"({column} ->> '{json_path}')::int"

    # -- text comparison -----------------------------------------------------
    def ci_equals(self, column: str, placeholder: str) -> str:
        """A case-insensitive equality WHERE fragment: ``column`` == bound value.

        Replaces SQLite's ``column = ? COLLATE NOCASE`` (COLLATE NOCASE is a
        SQLite-only clause that raises on Postgres). ``LOWER(col) = LOWER(<p>)``
        is equivalent for the ASCII-identifier data these callers compare
        (entity canonical names) and is valid on BOTH backends, so a single form
        is emitted for each — differing only in the placeholder token.

        ``ci_equals("canonical_name", self.param())`` →
        SQLite:   ``LOWER(canonical_name) = LOWER(?)``
        Postgres: ``LOWER(canonical_name) = LOWER(%s)``

        NOTE: this changes the SQLite SQL text from ``canonical_name = ? COLLATE
        NOCASE`` to a ``LOWER()``-based form. For ASCII data the match set is
        identical; unlike a bare ``= ? COLLATE NOCASE`` it is NOT index-backed by
        a plain index on ``canonical_name`` — acceptable here because these lookups
        are already bounded (single-entity resolution), not hot scans. ``column``
        is a trusted identifier; ``placeholder`` is the caller's bind marker.
        """
        return f"LOWER({column}) = LOWER({placeholder})"

    # -- temporal validity ---------------------------------------------------
    def temporal_open_clause(self, column: str, op: str) -> str:
        """A "validity bound is open OR satisfies `op`" WHERE fragment.

        The temporal-validity filter treats an unset bound as open-ended. In
        SQLite, unset is stored as either NULL or the empty string ''; in
        Postgres the column is TIMESTAMPTZ and '' is not a legal value (it
        raises ``invalid input syntax for type timestamp with time zone``), so
        the ``= ''`` disjunct MUST be dropped there — ``IS NULL`` already covers
        the unset case (the PG schema defaults these bounds to NULL).

        ``temporal_open_clause("mi.valid_from", "<=")`` →
        SQLite:   ``(mi.valid_from IS NULL OR mi.valid_from = '' OR mi.valid_from <= {p})``
        Postgres: ``(mi.valid_from IS NULL OR mi.valid_from <= {p})``

        ``op`` is a trusted comparison operator (``<=`` / ``>``); ``column`` is a
        trusted identifier. The caller binds one parameter for the ``op`` term.
        """
        if op not in ("<=", ">=", "<", ">", "="):
            raise ValueError(f"unexpected temporal operator {op!r}")
        p = self.param()
        if self.backend == "sqlite":
            return f"({column} IS NULL OR {column} = '' OR {column} {op} {p})"
        return f"({column} IS NULL OR {column} {op} {p})"

    def coalesce_open_timestamp(self, column: str, fill_placeholder: str) -> str:
        """COALESCE an "open" timestamp bound to a fill value, backend-correct.

        SQLite stores an unset bound as ``''`` (migration 010 default) OR NULL,
        so it needs ``COALESCE(NULLIF(col, ''), <fill>)`` to treat both as open.
        Postgres columns are TIMESTAMPTZ where ``''`` is not a legal value (the
        PG schema defaults these to NULL), so ``NULLIF(col, '')`` itself raises —
        a plain ``COALESCE(col, <fill>)`` is correct there.

        ``coalesce_open_timestamp("valid_to", "?")`` ->
        SQLite:   ``COALESCE(NULLIF(valid_to, ''), ?)``
        Postgres: ``COALESCE(valid_to, %s)``

        ``column`` is a trusted identifier; ``fill_placeholder`` is the caller's
        bind marker for the fill value (usually ``self.param()``).
        """
        if self.backend == "sqlite":
            return f"COALESCE(NULLIF({column}, ''), {fill_placeholder})"
        return f"COALESCE({column}, {fill_placeholder})"

    # -- introspection -------------------------------------------------------
    def table_exists(self, table: str) -> tuple[str, tuple]:
        """A (sql, params) pair whose result is truthy iff ``table`` exists.

        Replaces the ``SELECT 1 FROM sqlite_master WHERE type='table' AND
        name='t'`` probe (sqlite_master is a SQLite-only catalog). The query
        returns exactly one row when the table is present, zero rows otherwise,
        so the caller pattern is unchanged: ``db.execute(sql, params).fetchone()
        is not None``.

        SQLite:   ``SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?``
        Postgres: ``SELECT 1 WHERE to_regclass(%s) IS NOT NULL`` (schema-qualified
        name resolved against the search_path). ``table`` is a trusted identifier,
        bound as a parameter on both backends.
        """
        if not table.isidentifier():
            raise ValueError(f"table name must be a bare identifier: {table!r}")
        if self.backend == "sqlite":
            return (
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
                (table,),
            )
        # to_regclass returns NULL for a non-existent relation; the WHERE keeps the
        # result shape (0 or 1 rows) identical to the sqlite probe above.
        return ("SELECT 1 WHERE to_regclass(%s) IS NOT NULL", (table,))

    def columns_of(self, table: str) -> tuple[str, tuple]:
        """A (sql, params) pair listing a table's column names.

        The result row shape is IDENTICAL on both backends: the column NAME is at
        ``row[0]`` (one column per row, ordinal order), so a caller reads
        ``{r[0] for r in db.execute(sql, params)}`` unchanged on SQLite and PG.

        SQLite uses the ``pragma_table_info()`` table-valued function (not the
        bare ``PRAGMA table_info`` statement, whose name is at ``row[1]``) so the
        index matches Postgres's ``information_schema.columns.column_name``.
        ``table`` is a trusted identifier.
        """
        # The table name is interpolated into the SQLite pragma-function argument
        # (it can't be a bound literal there), so require a bare identifier on BOTH
        # backends for parity — even though the pg path binds it and is safe anyway.
        if not table.isidentifier():
            raise ValueError(f"table name must be a bare identifier: {table!r}")
        if self.backend == "sqlite":
            # pragma_table_info('t') is a table-valued function (SQLite >= 3.16);
            # its `name` column is the column name. Caller reads row[0].
            return (f"SELECT name FROM pragma_table_info('{table}')", ())
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
