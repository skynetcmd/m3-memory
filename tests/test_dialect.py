"""Phase 1a unit tests for the SQL dialect helpers — pure, no database.

Each divergence in plan §6 has one helper; each helper is asserted for BOTH
backends here so a mechanical port can trust a single API. These are the tests
that let Tier-1/Tier-2 passes run without a live Postgres.
"""
from __future__ import annotations

import pytest
from memory.backends.dialect import POSTGRES, SQLITE, Dialect, dialect_for


def test_dialect_for_returns_shared_singletons():
    assert dialect_for("sqlite") is SQLITE
    assert dialect_for("postgres") is POSTGRES
    with pytest.raises(ValueError):
        dialect_for("mysql")  # type: ignore[arg-type]


def test_dialect_is_frozen():
    with pytest.raises(Exception):  # dataclass frozen -> FrozenInstanceError
        SQLITE.backend = "postgres"  # type: ignore[misc]


class TestPlaceholder:
    def test_sqlite_qmark(self):
        assert SQLITE.placeholder(1) == "?"
        assert SQLITE.placeholder(3) == "?, ?, ?"

    def test_postgres_format(self):
        assert POSTGRES.placeholder(1) == "%s"
        assert POSTGRES.placeholder(3) == "%s, %s, %s"

    def test_matches_legacy_join_idiom(self):
        # the idiom this replaces: ",".join("?" * n)
        assert SQLITE.placeholder(4).replace(" ", "") == ",".join("?" * 4)

    def test_single_param(self):
        assert SQLITE.param() == "?"
        assert POSTGRES.param() == "%s"

    @pytest.mark.parametrize("d", [SQLITE, POSTGRES])
    def test_zero_fails_loud(self, d: Dialect):
        with pytest.raises(ValueError):
            d.placeholder(0)
        with pytest.raises(ValueError):
            d.placeholder(-1)


class TestUpsert:
    def test_insert_or_ignore_prefix(self):
        assert SQLITE.insert_or_ignore() == "INSERT OR IGNORE INTO"
        # postgres has no verb form: plain INSERT + trailing clause
        assert POSTGRES.insert_or_ignore() == "INSERT INTO"

    def test_on_conflict_ignore(self):
        assert SQLITE.on_conflict_ignore() == ""  # OR IGNORE already handled it
        assert POSTGRES.on_conflict_ignore() == "ON CONFLICT DO NOTHING"
        assert (
            POSTGRES.on_conflict_ignore(conflict_target="(id)")
            == "ON CONFLICT (id) DO NOTHING"
        )

    def test_on_conflict_ignore_partial_index_arbiter(self):
        # Postgres needs the partial index's WHERE predicate in the arbiter or
        # the conflict on a partial unique index is not caught.
        got = POSTGRES.on_conflict_ignore(
            conflict_target="(memory_id, source_kind, source_ref)",
            index_predicate="delta > 0",
        )
        assert got == (
            "ON CONFLICT (memory_id, source_kind, source_ref) "
            "WHERE delta > 0 DO NOTHING"
        )
        # SQLite ignores all of this (the OR IGNORE prefix handled it)
        assert SQLITE.on_conflict_ignore(
            conflict_target="(a, b)", index_predicate="x > 0"
        ) == ""

    def test_index_predicate_requires_target(self):
        with pytest.raises(ValueError):
            POSTGRES.on_conflict_ignore(index_predicate="delta > 0")

    def test_on_conflict_update_shared_excluded_syntax(self):
        # both backends use the `excluded` pseudo-table
        got = POSTGRES.on_conflict_update("(id)", ["content", "updated_at"])
        assert got == (
            "ON CONFLICT (id) DO UPDATE SET "
            "content = excluded.content, updated_at = excluded.updated_at"
        )
        assert SQLITE.on_conflict_update("(id)", ["content"]) == (
            "ON CONFLICT (id) DO UPDATE SET content = excluded.content"
        )

    def test_on_conflict_update_needs_columns(self):
        with pytest.raises(ValueError):
            POSTGRES.on_conflict_update("(id)", [])


class TestTime:
    def test_now_matches_existing_column_default(self):
        # SQLite default in the live schema is exactly this expression
        assert SQLITE.now() == "strftime('%Y-%m-%dT%H:%M:%SZ','now')"
        assert POSTGRES.now() == "NOW()"


class TestJson:
    def test_extract_text(self):
        assert (
            SQLITE.json_extract_text("metadata_json", "provider")
            == "json_extract(metadata_json, '$.provider')"
        )
        assert (
            POSTGRES.json_extract_text("metadata_json", "provider")
            == "metadata_json ->> 'provider'"
        )

    @pytest.mark.parametrize("d", [SQLITE, POSTGRES])
    def test_extract_rejects_quote_injection(self, d: Dialect):
        with pytest.raises(ValueError):
            d.json_extract_text("metadata_json", "x'; DROP TABLE")

    def test_extract_int(self):
        assert (
            SQLITE.json_extract_int("metadata_json", "session_idx")
            == "CAST(json_extract(metadata_json, '$.session_idx') AS INTEGER)"
        )
        assert (
            POSTGRES.json_extract_int("metadata_json", "session_idx")
            == "(metadata_json ->> 'session_idx')::int"
        )

    @pytest.mark.parametrize("d", [SQLITE, POSTGRES])
    def test_extract_int_rejects_quote_injection(self, d: Dialect):
        with pytest.raises(ValueError):
            d.json_extract_int("metadata_json", "x'; DROP TABLE")


class TestCiEquals:
    def test_lower_form_on_both_backends(self):
        # SQLite: replaces `col = ? COLLATE NOCASE`; PG: valid ILIKE-free equality.
        assert (
            SQLITE.ci_equals("canonical_name", SQLITE.param())
            == "LOWER(canonical_name) = LOWER(?)"
        )
        assert (
            POSTGRES.ci_equals("canonical_name", POSTGRES.param())
            == "LOWER(canonical_name) = LOWER(%s)"
        )

    def test_no_collate_nocase_leaks_to_postgres(self):
        # COLLATE NOCASE is a hard syntax error on PG — it must never appear.
        assert "COLLATE" not in POSTGRES.ci_equals("canonical_name", "%s")


class TestTemporalOpenClause:
    def test_sqlite_keeps_empty_string_disjunct_byte_identical(self):
        # MUST match the exact string that was hardcoded in search.py before the
        # refactor, so the SQLite path is a zero-behavior-change swap.
        assert SQLITE.temporal_open_clause("mi.valid_from", "<=") == (
            "(mi.valid_from IS NULL OR mi.valid_from = '' OR mi.valid_from <= ?)"
        )
        assert SQLITE.temporal_open_clause("mi.valid_to", ">") == (
            "(mi.valid_to IS NULL OR mi.valid_to = '' OR mi.valid_to > ?)"
        )

    def test_postgres_drops_empty_string_disjunct(self):
        # '' is not a legal TIMESTAMPTZ literal; only NULL + the op term remain.
        assert POSTGRES.temporal_open_clause("mi.valid_from", "<=") == (
            "(mi.valid_from IS NULL OR mi.valid_from <= %s)"
        )
        assert "= ''" not in POSTGRES.temporal_open_clause("mi.valid_to", ">")

    @pytest.mark.parametrize("d", [SQLITE, POSTGRES])
    def test_rejects_unexpected_operator(self, d: Dialect):
        with pytest.raises(ValueError):
            d.temporal_open_clause("mi.valid_from", "; DROP")


class TestCoalesceOpenTimestamp:
    def test_sqlite_keeps_nullif(self):
        # SQLite stores unset bounds as '' — must map '' -> NULL before COALESCE.
        assert SQLITE.coalesce_open_timestamp("valid_to", "?") == (
            "COALESCE(NULLIF(valid_to, ''), ?)"
        )

    def test_postgres_drops_nullif(self):
        # PG TIMESTAMPTZ rejects '' — NULLIF(col,'') would raise; plain COALESCE.
        got = POSTGRES.coalesce_open_timestamp("valid_to", "%s")
        assert got == "COALESCE(valid_to, %s)"
        assert "NULLIF" not in got


class TestTableExists:
    def test_sqlite_uses_sqlite_master(self):
        sql, params = SQLITE.table_exists("entity_embeddings")
        assert sql == (
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?"
        )
        assert params == ("entity_embeddings",)

    def test_postgres_uses_to_regclass_bound(self):
        sql, params = POSTGRES.table_exists("entity_embeddings")
        assert "to_regclass(%s)" in sql
        assert "sqlite_master" not in sql
        assert params == ("entity_embeddings",)

    @pytest.mark.parametrize("d", [SQLITE, POSTGRES])
    def test_rejects_quote(self, d: Dialect):
        with pytest.raises(ValueError):
            d.table_exists("t; DROP TABLE x")


class TestIntrospection:
    def test_sqlite_uses_pragma_function_name_at_index_0(self):
        # pragma_table_info() function form (not the bare PRAGMA statement) so the
        # column name is at row[0], matching PG's column_name — one caller index.
        sql, params = SQLITE.columns_of("memory_items")
        assert sql == "SELECT name FROM pragma_table_info('memory_items')"
        assert params == ()

    def test_postgres_uses_information_schema_parameterized(self):
        sql, params = POSTGRES.columns_of("memory_items")
        assert "information_schema.columns" in sql
        assert "column_name" in sql  # name at row[0], same as sqlite
        assert "%s" in sql  # table name is bound, not interpolated
        assert params == ("memory_items",)

    @pytest.mark.parametrize("d", [SQLITE, POSTGRES])
    def test_columns_of_rejects_quote(self, d: Dialect):
        with pytest.raises(ValueError):
            d.columns_of("memory_items; DROP")


class TestChatlogTableFork:
    """The chatlog table-name fork: memory_items on SQLite, chat_log_* on PG."""

    def test_sqlite_uses_core_names(self):
        from memory.backends.dialect import chatlog_table_for
        assert chatlog_table_for("items", "sqlite") == "memory_items"
        assert chatlog_table_for("embeddings", "sqlite") == "memory_embeddings"
        assert chatlog_table_for("extraction_queue", "sqlite") == "entity_extraction_queue"

    def test_postgres_uses_chat_log_names(self):
        from memory.backends.dialect import chatlog_table_for
        assert chatlog_table_for("items", "postgres") == "chat_log_items"
        assert chatlog_table_for("embeddings", "postgres") == "chat_log_embeddings"
        assert chatlog_table_for("item_entities", "postgres") == "chat_log_item_entities"
        assert chatlog_table_for("entity_rel", "postgres") == "chat_log_entity_relationships"
        assert chatlog_table_for("extraction_queue", "postgres") == "chat_log_extraction_queue"
        assert chatlog_table_for("chroma_sync_queue", "postgres") == "chat_log_chroma_sync_queue"

    def test_unknown_role_fails_loud(self):
        from memory.backends.dialect import chatlog_table_for
        with pytest.raises(KeyError):
            chatlog_table_for("nonexistent_role", "postgres")

    def test_every_role_maps_on_both_backends(self):
        from memory.backends.dialect import _CHATLOG_TABLES, chatlog_table_for
        for role in _CHATLOG_TABLES:
            s = chatlog_table_for(role, "sqlite")
            p = chatlog_table_for(role, "postgres")
            assert s and p
            assert p.startswith("chat_log_")  # every PG name is chat_log_ prefixed
            assert not s.startswith("chat_log_")  # sqlite keeps core names

    def test_no_fts_role(self):
        # memory_items_fts has no entry — FTS5 has no PG analogue.
        from memory.backends.dialect import _CHATLOG_TABLES
        assert "fts" not in _CHATLOG_TABLES
        assert not any("fts" in v[0] or "fts" in v[1] for v in _CHATLOG_TABLES.values())

    def test_active_backend_wrapper(self, monkeypatch):
        # chatlog_table() resolves the active backend.
        from memory.backends import selector as _sel
        monkeypatch.delenv("M3_DB_BACKEND", raising=False)
        _sel._reset_for_tests()
        from memory.backends.dialect import chatlog_table
        assert chatlog_table("items") == "memory_items"  # default sqlite


class TestEmptyJsonDefault:
    def test_sqlite_empty_string(self):
        # metadata_json is TEXT on SQLite; '' is valid and is the historical value.
        assert SQLITE.empty_json_default() == ""

    def test_postgres_empty_object(self):
        # JSONB rejects ''; '{}' is the empty object.
        assert POSTGRES.empty_json_default() == "{}"

    def test_sqlite_default_is_falsy_postgres_truthy(self):
        # Callers gate on truthiness: SQLite '' -> no-op (preserve prior behavior),
        # a JSON/JSONB backend '{}' -> normalize. A 3rd JSON backend returns '{}'.
        assert not SQLITE.empty_json_default()
        assert POSTGRES.empty_json_default()


class TestGeneratedIds:
    def test_returning_clause(self):
        assert SQLITE.returning_id_clause() == ""
        assert POSTGRES.returning_id_clause() == " RETURNING id"

    def test_last_insert_id_sqlite_uses_lastrowid(self):
        class _Cur:
            lastrowid = 42
            def fetchone(self):
                raise AssertionError("SQLite must NOT call fetchone for the id")
        assert SQLITE.last_insert_id(_Cur()) == 42

    def test_last_insert_id_postgres_uses_returning_row(self):
        class _Cur:
            lastrowid = None
            def fetchone(self):
                return (99,)  # RETURNING id row
        assert POSTGRES.last_insert_id(_Cur()) == 99
