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

    This is the BASE class. It holds the fields plus the methods whose output is
    IDENTICAL across backends (placeholder/param switch on ``param_style`` not
    backend; ci_equals emits one string for both; on_conflict_update uses the
    shared ``excluded`` pseudo-table). Every method whose SQL DIVERGES per backend
    is left abstract here — it raises ``NotImplementedError`` so a NEW backend that
    forgets to override it fails loudly rather than silently inheriting another
    backend's SQL. Concrete backends are :class:`SqliteDialect` /
    :class:`PostgresDialect`; adding a third is "add a subclass", no edits here.

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
        raise NotImplementedError("subclass must implement insert_or_ignore()")

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
        raise NotImplementedError("subclass must implement on_conflict_ignore()")

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
        raise NotImplementedError("subclass must implement now()")

    def now_minus_days(self, days_placeholder: str) -> str:
        """A "current time minus N days" expression; ``days_placeholder`` binds an
        INTEGER number of days (positive), NOT a SQLite modifier string.

        Replaces the SQLite-only ``datetime('now', ?)`` idiom (whose ``?`` bound a
        ``'-N days'`` modifier string that PG can't parse). Callers bind a plain
        int and get the right expression per backend:

            sql = f"... WHERE created_at < {_d.now_minus_days(_d.param())} ..."
            params = (..., stale_days, ...)   # an int, e.g. 5

        SQLite:   ``datetime('now', '-' || ? || ' days')``
        Postgres: ``NOW() - (%s * INTERVAL '1 day')``
        """
        raise NotImplementedError("subclass must implement now_minus_days()")

    # -- generated ids -------------------------------------------------------
    def returning_id_clause(self) -> str:
        """Trailing INSERT clause to make the statement return its generated id.

        SQLite has no ``RETURNING`` guarantee across all shipped versions, so it
        returns ``""`` and the id is read afterward via :meth:`last_insert_id`
        (``cur.lastrowid`` / ``last_insert_rowid()``). Postgres (and MariaDB 10.5+
        via its own ``RETURNING``) returns ``" RETURNING id"`` so the id comes back
        in the same round-trip. Pair with :meth:`last_insert_id`:

            cur = conn.execute(f"INSERT INTO t (...) VALUES ({ph}){_d.returning_id_clause()}", params)
            new_id = _d.last_insert_id(cur)
        """
        raise NotImplementedError("subclass must implement returning_id_clause()")

    def last_insert_id(self, cursor: object) -> object:
        """Read the id generated by the INSERT just executed on ``cursor``.

        Companion to :meth:`returning_id_clause`. On a RETURNING backend the id is
        the first column of the returned row (``cursor.fetchone()[0]``); on SQLite
        it is ``cursor.lastrowid``. Keyed on the backend's id-retrieval mechanism,
        NOT on ``== "postgres"`` — a 3rd backend supplies its own here.
        """
        raise NotImplementedError("subclass must implement last_insert_id()")

    # -- JSON ----------------------------------------------------------------
    def empty_json_default(self) -> "str | None":
        """The value to store for an EMPTY JSON column on this backend.

        SQLite's metadata_json is TEXT — an empty string ``''`` is fine and is the
        historical value. A native JSON/JSONB column (Postgres JSONB, MariaDB JSON)
        REJECTS ``''`` ("invalid input syntax for type json"), so an empty value
        must be the empty object ``'{}'``. Callers use this to normalize a blank
        metadata before an INSERT so the write works on every backend:

            if not (metadata and metadata.strip()):
                metadata = _d.empty_json_default()

        Keyed on the backend's JSON-column type, NOT on ``== "postgres"`` — a third
        JSON-typed SQL backend (MariaDB) gets ``'{}'`` too, not the SQLite ``''``.
        """
        raise NotImplementedError("subclass must implement empty_json_default()")

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
        # it must be a bare identifier (no quote, dot, bracket, semicolon). The
        # validation lives HERE (single-sourced); subclasses only supply the
        # backend expression via _json_extract_text_expr.
        if not json_path.isidentifier():
            raise ValueError(f"json_path must be a bare identifier: {json_path!r}")
        return self._json_extract_text_expr(column, json_path)

    def _json_extract_text_expr(self, column: str, json_path: str) -> str:
        """Backend fragment for :meth:`json_extract_text` (post-validation)."""
        raise NotImplementedError("subclass must implement _json_extract_text_expr()")

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
        # Validation single-sourced here; subclasses supply the backend expression.
        if not json_path.isidentifier():
            raise ValueError(f"json_path must be a bare identifier: {json_path!r}")
        return self._json_extract_int_expr(column, json_path)

    def _json_extract_int_expr(self, column: str, json_path: str) -> str:
        """Backend fragment for :meth:`json_extract_int` (post-validation)."""
        raise NotImplementedError("subclass must implement _json_extract_int_expr()")

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
        # Operator whitelist single-sourced here; subclass assembles the clause.
        if op not in ("<=", ">=", "<", ">", "="):
            raise ValueError(f"unexpected temporal operator {op!r}")
        p = self.param()
        return self._temporal_open_clause_expr(column, op, p)

    def _temporal_open_clause_expr(self, column: str, op: str, p: str) -> str:
        """Backend fragment for :meth:`temporal_open_clause` (post-validation)."""
        raise NotImplementedError(
            "subclass must implement _temporal_open_clause_expr()"
        )

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
        raise NotImplementedError("subclass must implement coalesce_open_timestamp()")

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
        # Validation single-sourced here; subclass supplies the backend probe.
        if not table.isidentifier():
            raise ValueError(f"table name must be a bare identifier: {table!r}")
        return self._table_exists_query(table)

    def _table_exists_query(self, table: str) -> tuple[str, tuple]:
        """Backend fragment for :meth:`table_exists` (post-validation)."""
        raise NotImplementedError("subclass must implement _table_exists_query()")

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
        # Validation single-sourced here; subclass supplies the backend query.
        if not table.isidentifier():
            raise ValueError(f"table name must be a bare identifier: {table!r}")
        return self._columns_of_query(table)

    def _columns_of_query(self, table: str) -> tuple[str, tuple]:
        """Backend fragment for :meth:`columns_of` (post-validation)."""
        raise NotImplementedError("subclass must implement _columns_of_query()")


# ── Concrete per-backend dialects ────────────────────────────────────────────
# Each subclass overrides ONLY the divergent methods/fragments, returning its
# backend's SQL verbatim. Adding a third SQL backend (e.g. MariaDB) is a new
# subclass here plus an entry in _BY_NAME — ZERO edits to the base method bodies.
# Fields are set as frozen-dataclass defaults so ``SqliteDialect()`` is fully
# specified and stays frozen (assignment raises FrozenInstanceError).


@dataclass(frozen=True)
class SqliteDialect(Dialect):
    """SQLite SQL surface (separate-file chatlog, qmark binds)."""

    backend: BackendName = "sqlite"
    param_style: ParamStyle = "qmark"

    def insert_or_ignore(self) -> str:
        return "INSERT OR IGNORE INTO"

    def on_conflict_ignore(
        self, *, conflict_target: str = "", index_predicate: str = ""
    ) -> str:
        return ""  # the OR IGNORE prefix already handled it

    def now(self) -> str:
        return "strftime('%Y-%m-%dT%H:%M:%SZ','now')"

    def now_minus_days(self, days_placeholder: str) -> str:
        # `?` binds an int; build the '-N days' modifier string in SQL.
        return f"datetime('now', '-' || {days_placeholder} || ' days')"

    def empty_json_default(self) -> "str | None":
        return ""  # metadata_json is TEXT on SQLite; '' is fine (historical value)

    def returning_id_clause(self) -> str:
        return ""  # id read afterward via last_insert_id (cur.lastrowid)

    def last_insert_id(self, cursor: object) -> object:
        return cursor.lastrowid  # type: ignore[attr-defined]

    def _json_extract_text_expr(self, column: str, json_path: str) -> str:
        return f"json_extract({column}, '$.{json_path}')"

    def _json_extract_int_expr(self, column: str, json_path: str) -> str:
        return f"CAST(json_extract({column}, '$.{json_path}') AS INTEGER)"

    def _temporal_open_clause_expr(self, column: str, op: str, p: str) -> str:
        return f"({column} IS NULL OR {column} = '' OR {column} {op} {p})"

    def coalesce_open_timestamp(self, column: str, fill_placeholder: str) -> str:
        return f"COALESCE(NULLIF({column}, ''), {fill_placeholder})"

    def _table_exists_query(self, table: str) -> tuple[str, tuple]:
        return (
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        )

    def _columns_of_query(self, table: str) -> tuple[str, tuple]:
        # pragma_table_info('t') is a table-valued function (SQLite >= 3.16);
        # its `name` column is the column name. Caller reads row[0].
        return (f"SELECT name FROM pragma_table_info('{table}')", ())


@dataclass(frozen=True)
class PostgresDialect(Dialect):
    """PostgreSQL SQL surface (one-schema chatlog, %s binds)."""

    backend: BackendName = "postgres"
    param_style: ParamStyle = "format"

    def insert_or_ignore(self) -> str:
        # postgres has no verb form — the arbiter goes in a trailing clause.
        return "INSERT INTO"

    def on_conflict_ignore(
        self, *, conflict_target: str = "", index_predicate: str = ""
    ) -> str:
        if index_predicate and not conflict_target:
            raise ValueError(
                "index_predicate requires a conflict_target (partial-index arbiter)"
            )
        tgt = f" {conflict_target}" if conflict_target else ""
        pred = f" WHERE {index_predicate}" if index_predicate else ""
        return f"ON CONFLICT{tgt}{pred} DO NOTHING"

    def now(self) -> str:
        return "NOW()"

    def now_minus_days(self, days_placeholder: str) -> str:
        # %s binds an int number of days; multiply a 1-day interval.
        return f"NOW() - ({days_placeholder} * INTERVAL '1 day')"

    def empty_json_default(self) -> "str | None":
        return "{}"  # metadata_json is JSONB; '' is rejected, '{}' is the empty obj

    def returning_id_clause(self) -> str:
        return " RETURNING id"  # no last_insert_rowid on PG; RETURNING is the way

    def last_insert_id(self, cursor: object) -> object:
        return cursor.fetchone()[0]  # type: ignore[attr-defined]

    def _json_extract_text_expr(self, column: str, json_path: str) -> str:
        return f"{column} ->> '{json_path}'"

    def _json_extract_int_expr(self, column: str, json_path: str) -> str:
        return f"({column} ->> '{json_path}')::int"

    def _temporal_open_clause_expr(self, column: str, op: str, p: str) -> str:
        return f"({column} IS NULL OR {column} {op} {p})"

    def coalesce_open_timestamp(self, column: str, fill_placeholder: str) -> str:
        return f"COALESCE({column}, {fill_placeholder})"

    def _table_exists_query(self, table: str) -> tuple[str, tuple]:
        # to_regclass returns NULL for a non-existent relation; the WHERE keeps the
        # result shape (0 or 1 rows) identical to the sqlite probe.
        return ("SELECT 1 WHERE to_regclass(%s) IS NOT NULL", (table,))

    def _columns_of_query(self, table: str) -> tuple[str, tuple]:
        sql = (
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s ORDER BY ordinal_position"
        )
        return (sql, (table,))


SQLITE = SqliteDialect()
POSTGRES = PostgresDialect()

_BY_NAME = {"sqlite": SQLITE, "postgres": POSTGRES}


def dialect_for(backend: BackendName) -> Dialect:
    """Return the shared :class:`Dialect` singleton for a backend name."""
    try:
        return _BY_NAME[backend]
    except KeyError:
        raise ValueError(f"no dialect for backend {backend!r}") from None


# ── Chatlog table-name fork (one-schema, two-table PG format) ────────────────
# On SQLite the chatlog store is a SEPARATE .db FILE whose tables reuse the SAME
# names as core (memory_items, ...). On PostgreSQL there is ONE database; chatlog
# rows live in DISTINCT `chat_log_*` tables in the same schema as core (the
# one-schema/two-table format — isolation of indexes/lifecycle/policy without the
# connection-routing cost of separate schemas). So chatlog SQL must emit
# `memory_items` on SQLite but `chat_log_items` on PG.
#
# This map is the single source of truth: LOGICAL role -> (core name, chat_log_*
# name). `chatlog_table(role)` resolves it for the active backend. Applied only at
# chatlog-subsystem query sites — `memory_items` in shared core code ALWAYS means
# core and is never routed through here. A map (not sql.replace) so it can't
# corrupt substrings like memory_item_entities / memory_items_fts / column names.
#
# THE RULE (N-backend safe): SQLite reuses the CORE table name (its chatlog is a
# separate .db file). EVERY NON-sqlite SQL backend uses the `chat_log_*` name —
# they share the one-schema/two-table model. So `chatlog_table_for` keys off the
# explicit predicate "backend == 'sqlite'", NOT an else==postgres accident: a 3rd
# SQL backend (e.g. MariaDB) correctly lands on the chat_log_* name with no edit
# to this map. The columns below are (core_name, chat_log_name), not (sqlite, pg).
#
# memory_items_fts (FTS5) has NO entry: it has no PG analogue; chatlog keyword
# search on PG uses chat_log_items.search_vector via the seam's keyword_search.
_CHATLOG_TABLES: dict[str, tuple[str, str]] = {
    #  role                core name (sqlite)         chat_log_* name (all non-sqlite)
    "items":             ("memory_items",            "chat_log_items"),
    "embeddings":        ("memory_embeddings",       "chat_log_embeddings"),
    "relationships":     ("memory_relationships",    "chat_log_relationships"),
    "entities":          ("entities",                "chat_log_entities"),
    "item_entities":     ("memory_item_entities",    "chat_log_item_entities"),
    "entity_rel":        ("entity_relationships",    "chat_log_entity_relationships"),
    "extraction_queue":  ("entity_extraction_queue", "chat_log_extraction_queue"),
}


def chatlog_table_for(role: str, backend: BackendName) -> str:
    """Physical chatlog table name for ``role`` on ``backend``.

    ``chatlog_table_for("items", "sqlite")`` -> ``"memory_items"``;
    ``chatlog_table_for("items", "postgres")`` -> ``"chat_log_items"``.
    The rule is explicit: SQLite gets the core name; EVERY other SQL backend gets
    the ``chat_log_*`` name (the shared one-schema model), so a future 3rd backend
    is correct without editing this function. Raises KeyError on an unknown role
    (fail loud — a typo shouldn't silently fall through to a core table name).
    """
    core_name, chat_log_name = _CHATLOG_TABLES[role]
    return core_name if backend == "sqlite" else chat_log_name


def chatlog_table(role: str) -> str:
    """Physical chatlog table name for ``role`` on the ACTIVE backend.

    Convenience wrapper over :func:`chatlog_table_for` that resolves the active
    backend. Use at chatlog-subsystem query sites:

        T = chatlog_table("items")
        sql = f"SELECT id FROM {T} WHERE ..."
    """
    from memory.backends import active_backend

    return chatlog_table_for(role, active_backend().name)
