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

import json as _json
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

    def day_bucket(self, column: str) -> str:
        """A 'YYYY-MM-DD' day-bucket expression over a timestamp ``column``, for
        GROUP BY day. ``column`` is a trusted identifier (never user input).

        Replaces the SQLite-only ``substr(created_at,1,10)`` idiom: PG's column is
        TIMESTAMPTZ with no substr(timestamptz,...) overload and no implicit
        text cast, so that raw form raised at query time. Both backends return the
        same TEXT 'YYYY-MM-DD' shape so the bucket value is identical across them.

        SQLite:   ``substr(<column>,1,10)``
        Postgres: ``to_char(<column>, 'YYYY-MM-DD')``
        """
        raise NotImplementedError("subclass must implement day_bucket()")

    def now_minus_minutes(self, minutes_placeholder: str) -> str:
        """A "current time minus N minutes" expression; ``minutes_placeholder``
        binds an INTEGER number of minutes (positive). The minutes analog of
        ``now_minus_days`` — used for short throughput windows (dashboard queue
        stats). SQLite: ``datetime('now', '-' || ? || ' minutes')``; Postgres:
        ``NOW() - (%s * INTERVAL '1 minute')``.
        """
        raise NotImplementedError("subclass must implement now_minus_minutes()")

    def age_days_gt(self, ts_column: str, days_expr: str) -> str:
        """Boolean predicate: the timestamp in ``ts_column`` is OLDER than
        ``days_expr`` days (i.e. now - ts > days). ``days_expr`` is either a bind
        placeholder (``self.param()``) or a trusted literal int; ``ts_column`` is a
        trusted identifier (never user input).

        Replaces the SQLite-only ``julianday('now') - julianday(<col>) > <days>``
        idiom, which raises on PostgreSQL (no julianday()). Used by the
        retention/decay/purge passes in memory_maintenance.

            sql = f"... WHERE {_d.age_days_gt('created_at', _d.param())} ..."
            params = (..., ttl_days, ...)   # an int

        SQLite:   ``(julianday('now') - julianday(<col>)) > <days_expr>``
        Postgres: ``<col> < NOW() - (<days_expr> * INTERVAL '1 day')``
        """
        raise NotImplementedError("subclass must implement age_days_gt()")

    def all_rows_after_offset(self, offset_placeholder: str) -> str:
        """A ``LIMIT ... OFFSET`` tail that returns ALL rows after ``offset`` (no
        upper bound) — the "keep newest N, take the rest" idiom.

        Replaces the SQLite-only ``LIMIT -1 OFFSET ?`` (where -1 means "no limit"),
        which is a syntax error on PostgreSQL. ``offset_placeholder`` binds an int.

        SQLite:   ``LIMIT -1 OFFSET ?``
        Postgres: ``LIMIT ALL OFFSET %s``
        """
        raise NotImplementedError("subclass must implement all_rows_after_offset()")

    # -- row scoping ---------------------------------------------------------
    def scope_predicates(
        self,
        *,
        table_alias: str = "mi",
        user_id: str = "",
        scope: str = "",
        type_filter: str = "",
        agent_filter: str = "",
    ) -> "tuple[str, list]":
        """Render the standard row-narrowing WHERE fragment + its bind params.

        Returns ``(" AND a = ? AND b = ?", [a, b])`` — a fragment that APPENDS to
        an existing WHERE, plus positional params in matching order. Empty
        arguments are skipped, so ``scope_predicates()`` with no filters returns
        ``("", [])`` and callers stay byte-identical to the unfiltered case.

        WHY THIS IS A SEAM PRIMITIVE, not a helper next to one call site: these
        four predicates are the tenancy + narrowing contract for every
        candidate-fetch path, and they had been hand-copied into three places in
        memory/search.py alone. They then DRIFTED — a 2026-07-14 tenancy fix
        added user_id/scope to all three copies, but type_filter/agent_filter
        were only ever wired into one. The visible symptom was
        ``memory_search(query="runbook", type_filter="procedure")`` returning
        chat_log and belief rows on BOTH SQLite and PostgreSQL, silently, at
        score 1.0. Duplicated predicate logic was the defect; centralising it
        here is the fix, and it means a future MariaDB/Mongo backend inherits
        correct scoping by subclassing rather than by remembering to patch N
        call sites.

        Placeholders come from :meth:`param`, so no call site hardcodes ``?``
        or ``%s``.

        Quoting convention (preserved from the original call sites): a quoted
        value (``'"procedure"'``) means exact equality; bare means ``LIKE``.
        Bare LIKE without wildcards IS equality, so a plain type name behaves
        identically either way, while a caller relying on LIKE patterns keeps
        working. ``agent_filter`` is matched case-insensitively when bare.
        """
        ph = self.param()
        a = table_alias
        sql = ""
        params: list = []

        def _quoted(v: str) -> bool:
            return (v.startswith('"') and v.endswith('"')) or (
                v.startswith("'") and v.endswith("'")
            )

        if user_id:
            sql += f" AND {a}.user_id = {ph}"
            params.append(user_id)
        if scope:
            sql += f" AND {a}.scope = {ph}"
            params.append(scope)
        if type_filter:
            exact = _quoted(type_filter)
            sql += f" AND {a}.type = {ph}" if exact else f" AND {a}.type LIKE {ph}"
            params.append(type_filter[1:-1] if exact else type_filter)
        if agent_filter:
            exact = _quoted(agent_filter)
            sql += (
                f" AND {a}.agent_id = {ph}" if exact
                else f" AND LOWER({a}.agent_id) = LOWER({ph})"
            )
            params.append(agent_filter[1:-1] if exact else agent_filter)

        return sql, params

    def group_concat(self, expr: str, separator: str = ",") -> str:
        """Aggregate ``expr`` across a GROUP BY into a single separator-joined
        string. ``expr`` is a trusted column/identifier; ``separator`` is a trusted
        literal (never user input).

        Replaces the SQLite-only ``GROUP_CONCAT(<expr>, '<sep>')``, which raises on
        PostgreSQL (uses ``string_agg``). Both return a single TEXT value.

        SQLite:   ``GROUP_CONCAT(<expr>, '<sep>')``
        Postgres: ``string_agg(<expr>, '<sep>')``
        """
        raise NotImplementedError("subclass must implement group_concat()")

    def greatest(self, *exprs: str) -> str:
        """Scalar "largest of the arguments" — e.g. clamp a value to a floor.

        SQLite spells this ``MAX(a, b, ...)`` (scalar, 2+ args) but PostgreSQL's
        ``MAX`` is an AGGREGATE; the PG scalar is ``GREATEST(a, b, ...)`` (and
        vice-versa: SQLite has no GREATEST). Args are trusted SQL fragments.

        SQLite:   ``MAX(a, b, ...)``     Postgres: ``GREATEST(a, b, ...)``
        """
        raise NotImplementedError("subclass must implement greatest()")

    def least(self, *exprs: str) -> str:
        """Scalar "smallest of the arguments" — e.g. clamp a value to a ceiling.
        The ``least`` analogue of :meth:`greatest`.

        SQLite:   ``MIN(a, b, ...)``     Postgres: ``LEAST(a, b, ...)``
        """
        raise NotImplementedError("subclass must implement least()")

    def is_undefined_object_error(self, exc: BaseException) -> bool:
        """True if ``exc`` is a "no such column / no such table / no such object"
        error — the class of failure that "tolerate a pre-migration DB, degrade to
        a no-op" code paths want to catch.

        Backends signal this DIFFERENTLY, and callers must NOT string-match the
        message themselves: SQLite raises ``sqlite3.OperationalError("no such
        column: x")`` while PostgreSQL raises ``psycopg2.errors.UndefinedColumn`` /
        ``UndefinedTable`` with stable SQLSTATE codes (``42703`` / ``42P01``).
        Matching the PG SQLSTATE is far more robust than its English message (which
        varies by version/locale). This predicate hides that split so a caller asks
        one portable question:

            except Exception as e:
                if not dialect().is_undefined_object_error(e):
                    raise            # a real error, not a pre-migration schema gap

        Also handles a wrapped/chained exception via ``__cause__`` so a
        seam-adapted error still classifies.
        """
        raise NotImplementedError(
            "subclass must implement is_undefined_object_error()")

    def is_integrity_error(self, exc: BaseException) -> bool:
        """True if ``exc`` is an INTEGRITY-constraint violation — a unique/primary-
        key/NOT-NULL/foreign-key conflict — the class of failure a "insert, and on a
        uniqueness race re-resolve the existing row" path wants to catch.

        Backends signal this DIFFERENTLY and callers must NOT type-check one
        backend's exception: SQLite raises ``sqlite3.IntegrityError`` while
        PostgreSQL raises ``psycopg2.IntegrityError`` (SQLSTATE class ``23xxx`` —
        ``23505`` unique_violation, ``23503`` foreign_key_violation, ``23502``
        not_null_violation, ``23514`` check_violation). Matching the PG SQLSTATE
        class is more robust than its message. This predicate hides the split:

            except Exception as e:
                if not dialect().is_integrity_error(e):
                    raise                 # a real error, not a constraint race
                existing = _find_existing(conn, name)  # re-resolve + continue

        Handles a wrapped/chained exception via ``__cause__`` so a seam-adapted
        error still classifies.
        """
        raise NotImplementedError(
            "subclass must implement is_integrity_error()")

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

    def json_column_to_dict(self, value: object) -> dict:
        """Normalize a FETCHED JSON column to a dict. The read-side complement
        to :meth:`empty_json_default` (which handles the write side).

        The same column comes back as a different Python type per backend:
        SQLite stores ``metadata_json`` as TEXT so the driver yields ``str``,
        while Postgres JSONB (and MariaDB JSON) yields an ALREADY-PARSED
        ``dict``. A bare ``json.loads()`` therefore works on SQLite and raises
        ``TypeError`` on Postgres — the trap documented on
        :meth:`json_extract_text`.

        Callers must route every read of a JSON column through this instead of
        writing their own str-or-dict shim: a call-site helper is the same
        defect in smaller form, because the next author copy-pastes it.

        Fails SAFE, never loud: ``None`` / empty / malformed all yield ``{}``.
        A caller gating on a field (e.g. rendering only ``authority ==
        "canonical"``) then treats unparseable metadata as "not authoritative"
        rather than crashing a whole batch render on one bad row.

        Concrete on the base class because normalization keys on the VALUE's
        Python type, not on SQL syntax — so it is already correct for any future
        backend whose driver returns either shape.
        """
        if value is None or value == "" or value == b"":
            return {}
        if isinstance(value, dict):
            return value
        if isinstance(value, (bytes, bytearray)):
            try:
                value = value.decode("utf-8")
            except Exception:
                return {}
        if isinstance(value, str):
            try:
                parsed = _json.loads(value)
            except Exception:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    def json_bind_value(self, value: object) -> "str | None":
        """Normalize a value for BINDING into a JSON column. The write-side
        complement to :meth:`json_column_to_dict` (which handles the read side).

        The round-trip trap this closes: a whole-column read of a Postgres JSONB
        column yields an already-parsed ``dict`` (see :meth:`json_column_to_dict`).
        Feeding that dict straight back into an INSERT/UPDATE bind — the natural
        shape of an export→import round-trip — raises psycopg2
        ``ProgrammingError: can't adapt type 'dict'``, because psycopg2 has no
        default adapter for a bare dict. SQLite's ``sqlite3`` refuses it too
        (``InterfaceError``). A JSON *string* binds correctly on BOTH backends:
        SQLite's column is TEXT so the string is stored verbatim, and Postgres
        implicitly casts a JSON-shaped ``str`` into JSONB. So the portable bind
        value is always the serialized string.

        Callers must route every bind of a JSON-column value through this instead
        of writing their own ``json.dumps(v) if isinstance(v, dict) else v`` shim
        at the call site — the same defect in smaller form, because the next
        author copy-pastes it (the rule for :meth:`json_column_to_dict`, applied
        to the write side).

        Coercion, keyed on the VALUE's Python type (never on SQL syntax, so it is
        already correct for any future JSON-typed backend):
          - ``None``                 -> ``None`` (SQL NULL; the column is nullable)
          - ``dict`` / ``list``      -> ``json.dumps(value)``
          - ``str``                  -> passed through verbatim (already serialized;
                                        re-dumping would double-encode it)
          - anything else            -> ``json.dumps(value)`` (numbers, bools)

        Note this does NOT substitute :meth:`empty_json_default` — an empty/blank
        metadata still needs that backend-specific default (``''`` vs ``'{}'``)
        because ``None`` here means SQL NULL, not "empty object". Normalize blanks
        with ``empty_json_default`` first if the column is NOT NULL.
        """
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return _json.dumps(value)

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

    def json_is_valid(self, column: str) -> str:
        """A boolean SQL expression: does ``column`` hold parseable JSON?

        ``json_is_valid("metadata_json")`` →
        SQLite:   ``json_valid(metadata_json)``
        Postgres: ``pg_input_is_valid(metadata_json::text, 'jsonb')``

        Completes the JSON family (``json_extract_text`` / ``json_extract_int``
        / ``json_column_to_dict`` / ``json_bind_value`` / ``empty_json_default``),
        which could read and write JSON but never ask whether a value IS JSON.

        Exists so a repair can be SET-BASED. The doctor's metadata repair used to
        SELECT every row, ``json.loads()`` each one in Python, and issue a
        per-row UPDATE for the failures. On SQLite that is merely wasteful; on a
        pooled backend it ships the whole table across the wire and pays N
        round-trips (§4 efficiency, §8 performance). With this expression the
        same repair is one statement the server evaluates in place.

        NULL semantics are the caller's to handle: SQLite's ``json_valid(NULL)``
        yields NULL, not 0, so guard with ``IS NOT NULL`` when NULL means "no
        metadata" rather than "corrupt metadata".
        """
        return self._json_is_valid_expr(column)

    def _json_is_valid_expr(self, column: str) -> str:
        """Backend fragment for :meth:`json_is_valid`."""
        raise NotImplementedError("subclass must implement _json_is_valid_expr()")

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

    def glob_match(self, column: str, placeholder: str, pattern: str) -> "tuple[str, str]":
        """A case-sensitive shell-glob filename match. Returns ``(sql_fragment,
        bound_value)`` — because the two backends differ in BOTH the operator AND
        the wildcard alphabet, so the VALUE may need rewriting too, not just the SQL.

        SQLite has a native ``GLOB`` operator (``*`` any run, ``?`` one char,
        case-sensitive) — the natural fit for a filename filter. PostgreSQL has no
        ``GLOB``; the closest case-sensitive form is ``LIKE`` with its own wildcards
        (``%`` any run, ``_`` one char). So on PG the fragment becomes ``LIKE`` and
        the glob wildcards in the pattern are translated to LIKE wildcards (with any
        literal ``%``/``_`` in the source pattern escaped first so they stay
        literal). SQLite passes the pattern through unchanged.

        ``glob_match("filename", param(), "*.md")`` →
          SQLite:   (``"filename GLOB ?"``,   ``"*.md"``)
          Postgres: (``"filename LIKE %s"``,   ``"%.md"``)

        Concrete on the base as a validate-then-delegate wrapper: ``column`` must be
        a bare identifier (single-sourced here); the backend supplies the operator +
        value transform via :meth:`_glob_fragment`.
        """
        if not column.isidentifier():
            raise ValueError(f"column must be a bare identifier: {column!r}")
        return self._glob_fragment(column, placeholder, pattern)

    def _glob_fragment(self, column: str, placeholder: str, pattern: str) -> "tuple[str, str]":
        """Backend fragment for :meth:`glob_match` (post-validation)."""
        raise NotImplementedError("subclass must implement _glob_fragment()")

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

    def column_exists(self, table: str, column: str) -> tuple[str, tuple]:
        """A (sql, params) pair whose result is truthy iff ``table`` has ``column``.

        The seam answer to "does this column exist?" — use it INSTEAD of running a
        query that references the column and catching the error. On PostgreSQL an
        ``UndefinedColumn`` error aborts the whole transaction, so any fallback
        query on the same connection then dies with ``InFailedSqlTransaction``;
        an up-front existence check has no such hazard and is backend-uniform. A
        missing table returns zero rows (no error), so this also covers "table not
        present yet". Caller pattern: ``bool(db.execute(sql, params).fetchall())``.
        """
        if not table.isidentifier():
            raise ValueError(f"table name must be a bare identifier: {table!r}")
        if not column.isidentifier():
            raise ValueError(f"column name must be a bare identifier: {column!r}")
        return self._column_exists_query(table, column)

    def _column_exists_query(self, table: str, column: str) -> tuple[str, tuple]:
        """Backend fragment for :meth:`column_exists` (post-validation)."""
        raise NotImplementedError("subclass must implement _column_exists_query()")

    # -- schema namespacing --------------------------------------------------
    def qualified_table(self, name: str, *, schema: str) -> str:
        """Qualify a table name that lives in a SEPARATE logical store.

        Some stores (the files-ingestion store, ``files_memory``) are physically
        separate from the primary memory store. On SQLite that separation is a
        separate database FILE, so a table reference is just the bare name — there
        is no schema concept and the file is opened directly. On PostgreSQL the
        separation is a schema NAMESPACE inside the one primary database (single
        DSN), so the same table is ``<schema>.<name>``.

        WHY qualification, not ``search_path``. A pooled PG connection that ran
        ``SET search_path = files, public`` keeps that setting after it returns to
        the pool — the next borrower silently resolves unqualified names against
        ``files``, so a process that alternates between the primary (``public``)
        and files (``files``) stores corrupts or errors depending on which schema
        it happens to land in. ``SET LOCAL`` reverts at COMMIT but makes
        correctness DEPEND on transaction discipline. Fully qualifying every name
        removes the failure mode STRUCTURALLY: there is no session state to leak
        and no "current schema" mode, so arbitrary mixing of ``public`` and
        ``files`` tables on ONE connection — even in one transaction — is always
        correct. This is the standard multi-schema approach (SQLAlchemy
        ``Table(schema=...)``, schema-qualified ``table_name``).

        ``qualified_table("leaves", schema="files")`` →
          SQLite:   ``leaves``          (separate DB file; schema ignored)
          Postgres: ``files.leaves``

        Both ``name`` and ``schema`` are trusted bare identifiers (they name our
        own tables/schemas, never end-user input) — validated here so a subclass
        cannot be handed an injection vector. Emitted UNQUOTED (lowercase
        identifiers on both backends), matching how every other table name in the
        codebase is written.
        """
        if not name.isidentifier():
            raise ValueError(f"table name must be a bare identifier: {name!r}")
        if not schema or not schema.isidentifier():
            raise ValueError(f"schema must be a bare identifier: {schema!r}")
        return self._qualified_table_expr(name, schema)

    def _qualified_table_expr(self, name: str, schema: str) -> str:
        """Backend fragment for :meth:`qualified_table` (post-validation).

        ABSTRACT — every dialect states its own qualification. "Bare name" is NOT
        a universal default: it is the SQLite-FAMILY answer (separation is a
        separate DB file, so there is no schema to qualify), not a correct default
        for a schema/namespaced backend. Postgres qualifies with the schema; a
        future MariaDB files store would qualify too; a document store wouldn't use
        SQL table names at all. Making the base raise (not silently return the bare
        name) means a new backend that forgets to implement this FAILS LOUD at first
        use — "add your dialect's form" — rather than inheriting SQLite's answer and
        silently targeting the wrong place. Same contract as insert_or_ignore /
        on_conflict_ignore / the other divergent primitives.
        """
        raise NotImplementedError("subclass must implement _qualified_table_expr()")

    # -- storage maintenance -------------------------------------------------
    def compact_storage(self, *, sqlite_path: "str | None" = None,
                        max_bytes: int = 500 * 1024 * 1024) -> str:
        """Reclaim free space in the store, if the backend has a client-issued
        compaction. Returns a human-readable status line for the maintenance report.

        This exists so the maintenance pass NEVER branches on
        ``resolve_backend_name() != "sqlite"`` at the call site. That call-site
        ``if not sqlite`` is a latent bug: a THIRD backend that DOES have a
        client-issued compaction — MariaDB ``OPTIMIZE TABLE``, or a future store —
        would fall into the PG "no-op" side and silently never be compacted. The
        decision belongs to the dialect, which knows its own storage engine.

        Contract, per backend:
          - **SQLite** — ``VACUUM`` rewrites the DB file to reclaim free pages. It
            must run OUTSIDE any open transaction and needs the *active* file path
            (which may differ from an import-time constant when a caller has switched
            databases), so callers pass it as ``sqlite_path``. Size-gated by
            ``max_bytes`` (default 500 MB, issue #46) to avoid multi-minute hangs.
          - **Postgres** — no-op: autovacuum reclaims space in the background and
            there is no meaningful client-issued file compaction (a manual
            ``VACUUM`` here would need its own out-of-txn connection and does not
            shrink the file without ``VACUUM FULL``, which takes an exclusive lock).
          - **A future JSON/row store (MariaDB, …)** — implements whatever its
            engine offers (``OPTIMIZE TABLE``) or returns its own no-op line. The
            point is that adding it is ONE method here, not editing every call site.

        Concrete on the base class as a safe DEFAULT no-op so a backend that adds
        nothing still works; SQLite overrides it. ``sqlite_path`` is ignored by
        every non-SQLite backend.
        """
        return "Compaction skipped: not applicable on this backend"


# ── Concrete per-backend dialects live in their backend modules ──────────────
# `SqliteDialect`/`SQLITE` are in `sqlite_backend.py`; `PostgresDialect`/`POSTGRES`
# in `postgres_backend.py` — co-located so adding a backend is ONE file
# (DESIGN_PHILOSOPHIES §2). This module holds ONLY the base `Dialect` above and
# holds NO reference to any concrete dialect, which is what keeps the import graph
# acyclic (RH1): `<name>_backend.py` imports the base `Dialect` from here and
# registers its singleton with the registry; `dialect_for` reads it back from the
# registry lazily. `dialect.py -> registry.py`, never `dialect.py -> *_backend.py`.


def dialect_for(backend: BackendName) -> Dialect:
    """Return the shared :class:`Dialect` singleton for a backend name.

    Delegates to the registry, which imports the backend module on demand so its
    ``@register_backend`` runs. This function never imports a concrete dialect
    class itself — that is the cycle-break (RH1). Raises ``ValueError`` for an
    unregistered backend (fail loud, §3).
    """
    from .registry import dialect_singleton_for

    return dialect_singleton_for(backend)


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
