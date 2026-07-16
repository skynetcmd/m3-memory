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


class TestIntrospection:
    def test_sqlite_uses_pragma_no_params(self):
        sql, params = SQLITE.columns_of("memory_items")
        assert sql == "PRAGMA table_info(memory_items)"
        assert params == ()

    def test_postgres_uses_information_schema_parameterized(self):
        sql, params = POSTGRES.columns_of("memory_items")
        assert "information_schema.columns" in sql
        assert "%s" in sql  # table name is bound, not interpolated
        assert params == ("memory_items",)

    @pytest.mark.parametrize("d", [SQLITE, POSTGRES])
    def test_columns_of_rejects_quote(self, d: Dialect):
        with pytest.raises(ValueError):
            d.columns_of("memory_items; DROP")
