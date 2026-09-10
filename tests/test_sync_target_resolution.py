"""Sync targets resolve from the BACKEND, not the filesystem.

The deployment has a different SHAPE on each backend, and this is the one part
of the PG-local-sync port that is genuine design rather than substitution:

  SQLite    TWO stores (agent_memory.db + agent_chatlog.db), each using the CORE
            table names — the chatlog is a separate file running its own
            migration chain, so both contain `memory_items`.
  Postgres  ONE store holding BOTH families: core `memory_items` alongside the
            chatlog clones `chat_log_items`, `chat_log_embeddings`, … in the same
            schema.

⚠ Getting this wrong does NOT crash. A port that kept iterating file paths would
sync the wrong table set on PostgreSQL and report success — which is why the
resolved (store, table-set) pairs are asserted per backend rather than left to
be inferred from a passing sync.

Hermetic: the backend is stubbed, so neither branch needs a live store.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

import pg_sync  # noqa: E402

_ROLES = ("items", "embeddings", "relationships")


class _FakeBackend:
    def __init__(self, name):
        self.name = name


@pytest.fixture()
def as_backend(monkeypatch):
    """Pretend the local store is a given backend, without needing one."""

    def _apply(name):
        monkeypatch.setattr(pg_sync, "_local_backend", lambda: _FakeBackend(name))

    return _apply


class TestPostgresShape:
    """ONE store, TWO table families."""

    def test_yields_one_store_with_two_table_sets(self, as_backend):
        as_backend("postgres")
        targets = pg_sync.resolve_sync_targets("postgresql://u@h/db")

        assert [t.name for t in targets] == ["main", "chatlog"]
        assert targets[0].uri == targets[1].uri, (
            "on PostgreSQL both families live in ONE store; separate URIs would "
            "mean the port still thinks in files"
        )

    def test_core_family_uses_core_names(self, as_backend):
        as_backend("postgres")
        core = pg_sync.resolve_sync_targets("postgresql://u@h/db")[0]
        assert core.table("items") == "memory_items"
        assert core.table("embeddings") == "memory_embeddings"

    def test_chatlog_family_uses_chat_log_names(self, as_backend):
        """The distinction that does not exist on SQLite.

        If these resolved to `memory_items` on PG, the chatlog pass would sync
        the CORE table a second time — silently doubling work and, worse,
        merging chatlog rows into core memory.
        """
        as_backend("postgres")
        chat = pg_sync.resolve_sync_targets("postgresql://u@h/db")[1]
        assert chat.table("items") == "chat_log_items"
        assert chat.table("embeddings") == "chat_log_embeddings"
        assert chat.table("relationships") == "chat_log_relationships"

    def test_a_future_backend_inherits_the_postgres_shape(self, as_backend):
        """Keyed on `!= sqlite`, not `== postgres`.

        `chatlog_table_for` already keys off an explicit `backend == "sqlite"`
        predicate so a third SQL backend lands on chat_log_* names with no edit;
        this asserts the target resolver follows the same rule rather than
        enumerating known backends.
        """
        as_backend("mariadb")
        targets = pg_sync.resolve_sync_targets("mysql://u@h/db")
        assert len(targets) == 2
        assert targets[0].uri == targets[1].uri
        assert targets[1].table("items") == "chat_log_items"


class TestSqliteShape:
    """TWO stores, CORE names in both."""

    def test_yields_a_target_per_file(self, as_backend, monkeypatch):
        as_backend("sqlite")

        class _T:
            def __init__(self, name, path):
                self.name, self.db_path = name, path

        import migrate_memory

        monkeypatch.setattr(
            migrate_memory, "targets",
            lambda _sel="all": [_T("main", "/m/main.db"), _T("chatlog", "/m/chat.db")],
        )
        targets = pg_sync.resolve_sync_targets()

        assert [t.name for t in targets] == ["main", "chatlog"]
        assert targets[0].uri != targets[1].uri, "separate FILES on SQLite"

    def test_both_files_use_core_table_names(self, as_backend, monkeypatch):
        """SQLite's chatlog reuses the core names in its own file.

        So `chat_log_items` must NOT appear here — that name exists only where
        both families share one schema.
        """
        as_backend("sqlite")

        class _T:
            def __init__(self, name, path):
                self.name, self.db_path = name, path

        import migrate_memory

        monkeypatch.setattr(
            migrate_memory, "targets",
            lambda _sel="all": [_T("main", "/m/main.db"), _T("chatlog", "/m/chat.db")],
        )
        for t in pg_sync.resolve_sync_targets():
            for role in _ROLES:
                assert not t.table(role).startswith("chat_log_"), (
                    f"{t.name}.{role} resolved to {t.table(role)!r}; SQLite uses "
                    "core names in a separate file"
                )

    def test_unresolvable_chatlog_still_syncs_main(self, as_backend, monkeypatch):
        """Skipping a store we cannot find is right; refusing to sync the one we
        CAN find because of it is not."""
        as_backend("sqlite")
        import migrate_memory

        def _boom(_sel="all"):
            raise RuntimeError("chatlog config unreadable")

        monkeypatch.setattr(migrate_memory, "targets", _boom)
        targets = pg_sync.resolve_sync_targets("/m/main.db")
        assert [t.name for t in targets] == ["main"]
        assert targets[0].table("items") == "memory_items"


def test_table_names_come_from_the_dialect_not_a_local_literal():
    """§2: one source for the naming.

    Restating "memory_items" in the resolver would give the codebase a second
    place to update when a table is renamed, and the two would drift.

    Inspects STRING CONSTANTS via the AST, not the raw source: the function's own
    comment says it does not restate "memory_items" here, and a substring check
    fails on that comment — it cannot tell code from prose about code. (Fifth
    instance of that trap in this work. The comment exists precisely to warn
    about the thing, which is what makes it so easy to trip over.)
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(pg_sync.resolve_sync_targets)))
    literals = {
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}

    assert "chatlog_table_for" in names, "the resolver must ask the dialect"
    hardcoded = {t for t in literals if t.startswith(("memory_", "chat_log_"))}
    assert not hardcoded, f"table names restated in the resolver: {hardcoded}"


def test_unmapped_role_falls_back_to_its_own_name():
    """A role the map does not cover must not resolve to None or blow up."""
    t = pg_sync.SyncTarget("x", None, {"items": "memory_items"})
    assert t.table("items") == "memory_items"
    assert t.table("tasks") == "tasks"


class TestTheResolverIsActuallyWired:
    """A correct resolver that nothing calls is dead code.

    It was, for one commit: `resolve_sync_targets` was defined, tested and never
    reached, so PG-local sync still could not work. These pin the wiring itself.
    """

    def test_main_uses_the_resolver_not_the_file_loop(self):
        import ast
        import inspect
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(pg_sync.main)))
        called = {
            n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        } | {
            n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        assert "resolve_sync_targets" in called
        assert "targets" not in called, (
            "migrate_memory.targets() yields FILE paths; using it here is the "
            "file-shaped port that syncs the wrong table set on PostgreSQL"
        )

    def test_no_raw_sqlite_connect_remains(self):
        """The local half opens through the seam.

        Asserted on the AST: the module's own comments discuss sqlite3.connect
        by name when explaining what replaced it, so a substring check fails on
        the explanation.
        """
        import ast

        src = (_BIN / "pg_sync.py").read_text(encoding="utf-8")
        connects = [
            n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr == "connect"
            and isinstance(n.func.value, ast.Name) and n.func.value.id == "sqlite3"
        ]
        assert not connects, f"{len(connects)} raw sqlite3.connect call(s) remain"

    def test_drift_exemption_was_removed(self):
        """The exemption and the last raw connect must go in the SAME commit.

        test_exempt_entries_actually_contain_the_idiom fails on an exemption for
        a file with no raw connect, so leaving it would break the suite; removing
        it early would have left the gate red while the port was in flight.
        """
        src = (
            _BIN.parent / "tests" / "test_raw_connection_drift.py"
        ).read_text(encoding="utf-8")
        assert '"bin/pg_sync.py"' not in src
