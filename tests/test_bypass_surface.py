"""ADR-0001 materialized bypass-surface — migration, builder, read-path, GDPR.

Covers the §11 validation plan items that don't need the full retrieval stack:
migration up/down + index seek, builder modes + scope gating, scope isolation,
and gdpr_forget removal via explicit enumeration (NOT cascade-only).
"""
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "bin"))

MIG_UP = (REPO / "memory" / "migrations" / "033_bypass_surface.up.sql").read_text()
MIG_DOWN = (REPO / "memory" / "migrations" / "033_bypass_surface.down.sql").read_text()


def _seed(db):
    """Minimal schema the surface touches: memory_items + entity link tables + the table."""
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("CREATE TABLE memory_items (id TEXT PRIMARY KEY, title TEXT, type TEXT, "
               "conversation_id TEXT, user_id TEXT, scope TEXT)")
    db.execute("CREATE TABLE entities (id TEXT PRIMARY KEY, entity_type TEXT)")
    # mie carries confidence + created_at (the ordering signals) — matches migration 024.
    db.execute("CREATE TABLE memory_item_entities (memory_id TEXT, entity_id TEXT, "
               "confidence REAL, created_at TEXT)")
    db.executescript(MIG_UP)


# ── Migration ────────────────────────────────────────────────────────────────

def test_migration_up_creates_table_and_index():
    db = sqlite3.connect(":memory:")
    _seed(db)
    assert db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                      "AND name='bypass_surface'").fetchone()
    assert db.execute("SELECT name FROM sqlite_master WHERE type='index' "
                      "AND name='idx_bypass_surface_scope'").fetchone()
    cols = [r[1] for r in db.execute("PRAGMA table_info(bypass_surface)")]
    assert cols == ["conversation_id", "memory_id", "source", "strategy",
                    "user_id", "scope", "cap", "built_at"]


def test_read_path_uses_index():
    db = sqlite3.connect(":memory:")
    _seed(db)
    plan = " ".join(
        r[-1] for r in db.execute(
            "EXPLAIN QUERY PLAN SELECT memory_id, source FROM bypass_surface "
            "WHERE conversation_id=? AND scope=?", ("c", "agent"))
    )
    assert "idx_bypass_surface_scope" in plan  # §8 indexed seek, not a scan


def test_migration_down_drops_table():
    db = sqlite3.connect(":memory:")
    _seed(db)
    db.executescript(MIG_DOWN)
    assert db.execute("SELECT name FROM sqlite_master WHERE type='table' "
                      "AND name='bypass_surface'").fetchone() is None


def test_fk_cascade_removes_surface_on_item_delete():
    db = sqlite3.connect(":memory:")
    _seed(db)
    db.execute("INSERT INTO memory_items VALUES ('m1','t','message','cA','u1','agent')")
    db.execute("INSERT INTO bypass_surface (conversation_id, memory_id, source, scope) "
               "VALUES ('cA','m1','entity','agent')")
    db.execute("DELETE FROM memory_items WHERE id='m1'")
    assert db.execute("SELECT COUNT(*) FROM bypass_surface").fetchone()[0] == 0


# ── Builder ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def built_db(monkeypatch):
    """A seeded DB with the builder wired to it via monkeypatched _db()."""
    import contextlib

    import memory.entity as E
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    _seed(db)
    for i in range(5):
        db.execute("INSERT INTO memory_items VALUES (?,?,?,?,?,?)",
                   (f"a{i}", "t", "message", "convA", "u1", "agent"))
    for i in range(3):
        db.execute("INSERT INTO memory_items VALUES (?,?,?,?,?,?)",
                   (f"b{i}", "t", "message", "convB", "u1", "agent"))
    db.execute("INSERT INTO entities VALUES ('e1','person')")
    for i in range(5):
        db.execute("INSERT INTO memory_item_entities VALUES (?, 'e1', ?, '2026-01-01')",
                   (f"a{i}", 0.9 - i * 0.1))
    for i in range(3):
        db.execute("INSERT INTO memory_item_entities VALUES (?, 'e1', 0.8, '2026-01-01')", (f"b{i}",))
    db.commit()

    @contextlib.contextmanager
    def fake_db():
        yield db
    monkeypatch.setattr(E, "_db", fake_db)
    return E, db


# Caller-supplied per-category rule table (core embeds no type rules): map each
# conversation's category to entity types to surface. Both scopes are 'person'-typed.
_RULES = {"person": ("person",)}
_CATS = {"convA": "person", "convB": "person"}


def test_builder_full_with_strategy_gating(built_db):
    E, db = built_db
    res = E.build_bypass_surface(strategy_for={"convA": "COMPUTE", "convB": "ASSISTANT"},
                                 category_for=_CATS, type_rules=_RULES)
    assert res["mode"] == "full"
    assert res["scopes_skipped_off_policy"] == 1  # ASSISTANT off
    # convA (COMPUTE, on) surfaces all 5; convB (ASSISTANT, off) surfaces none
    assert db.execute("SELECT COUNT(*) FROM bypass_surface WHERE conversation_id='convA'").fetchone()[0] == 5
    assert db.execute("SELECT COUNT(*) FROM bypass_surface WHERE conversation_id='convB'").fetchone()[0] == 0


def test_builder_incremental_only_touches_given_scope(built_db):
    E, db = built_db
    E.build_bypass_surface(strategy_for={"convA": "COMPUTE", "convB": "COMPUTE"},
                           category_for=_CATS, type_rules=_RULES)
    before_b = db.execute("SELECT COUNT(*) FROM bypass_surface WHERE conversation_id='convB'").fetchone()[0]
    # incremental rebuild of ONLY convA must not disturb convB
    E.build_bypass_surface(conversation_ids=["convA"], strategy_for={"convA": "COMPUTE"},
                           category_for=_CATS, type_rules=_RULES)
    assert db.execute("SELECT COUNT(*) FROM bypass_surface WHERE conversation_id='convB'").fetchone()[0] == before_b
    assert db.execute("SELECT COUNT(*) FROM bypass_surface WHERE conversation_id='convA'").fetchone()[0] == 5


def test_builder_empty_conversation_id_raises(built_db):
    E, _ = built_db
    with pytest.raises(ValueError):
        E.build_bypass_surface(conversation_ids=[""])  # §7: no global scan


def test_cap_ordering_keeps_highest_confidence(built_db):
    """Under a binding cap, the deterministic 'confidence' ordering keeps the
    highest-confidence mentions — not a planner-dependent arbitrary set."""
    E, db = built_db
    # convA items a0..a4 have descending confidence 0.9,0.8,0.7,0.6,0.5. cap=2 -> a0,a1.
    E.build_bypass_surface(conversation_ids=["convA"], strategy_for={"convA": "COMPUTE"},
                           category_for=_CATS, type_rules=_RULES, cap=2, order_by="confidence")
    kept = {r[0] for r in db.execute(
        "SELECT memory_id FROM bypass_surface WHERE conversation_id='convA'")}
    assert kept == {"a0", "a1"}  # the two highest-confidence, deterministically


def test_cap_env_default_and_override(built_db, monkeypatch):
    E, _ = built_db
    monkeypatch.delenv("M3_BYPASS_SURFACE_CAP", raising=False)
    assert E._resolve_bypass_cap() == 300
    monkeypatch.setenv("M3_BYPASS_SURFACE_CAP", "50")
    assert E._resolve_bypass_cap() == 50
    monkeypatch.setenv("M3_BYPASS_SURFACE_CAP", "garbage")
    assert E._resolve_bypass_cap() == 300  # bad input falls back to default


# ── GDPR (explicit enumeration, not cascade-only) ─────────────────────────────

def test_gdpr_forget_removes_surface_rows_by_user(monkeypatch):
    """gdpr_forget must purge bypass_surface via the explicit DELETE — even when the
    user's memory_items are removed in the same pass (the FK alone would also fire,
    but the explicit enumeration is what the impl relies on)."""
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    # broader schema gdpr_forget touches
    db.executescript(
        "CREATE TABLE memory_items (id TEXT PRIMARY KEY, user_id TEXT, conversation_id TEXT, scope TEXT, expires_at TEXT);"
        "CREATE TABLE memory_embeddings (memory_id TEXT);"
        "CREATE TABLE memory_relationships (from_id TEXT, to_id TEXT);"
        "CREATE TABLE memory_history (memory_id TEXT);"
        "CREATE TABLE gdpr_requests (id TEXT, subject_id TEXT, request_type TEXT, status TEXT, items_affected INT, completed_at TEXT);"
    )
    db.executescript(MIG_UP)
    db.execute("INSERT INTO memory_items VALUES ('m1','victim','cA','agent',NULL)")
    db.execute("INSERT INTO bypass_surface (conversation_id, memory_id, source, user_id, scope) "
               "VALUES ('cA','m1','entity','victim','agent')")
    db.commit()

    import contextlib

    import memory_maintenance as M

    @contextlib.contextmanager
    def fake_db():
        yield db
    # gdpr_forget_impl uses a _db() context — patch whatever it imports
    monkeypatch.setattr(M, "_db", fake_db, raising=False)
    # some builds resolve _db lazily; also patch the db module if present
    try:
        import importlib
        dbmod = importlib.import_module("memory.db")
        monkeypatch.setattr(dbmod, "_db", fake_db, raising=False)
    except Exception:
        pass

    M.gdpr_forget_impl("victim")
    assert db.execute("SELECT COUNT(*) FROM bypass_surface WHERE user_id='victim'").fetchone()[0] == 0
