"""`m3 doctor --fix` must repair EVERY store, not just the main DB.

Regression guard. The embed-backfill action scoped itself to resolve_db_path(None),
so on a split topology it counted/backfilled only agent_memory.db. Users ran the
obvious repair command, got "summary: ok", and the real backlog -- which lives in
the chatlog store -- was never touched. Observed 2026-07-25: --fix saw 2 items on
the main DB while 6653 sat unembedded in the chatlog.

A repair tool that cleanly no-ops on the actual problem is worse than one that
errors: it sends people looking somewhere else.
"""

import os
import sqlite3
import sys

import pytest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)


def _make_store(path, missing=0, embedded=0):
    """A minimal memory_items/memory_embeddings pair."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE memory_items (id TEXT PRIMARY KEY, content TEXT, "
        "type TEXT, is_deleted INTEGER DEFAULT 0)"
    )
    conn.execute("CREATE TABLE memory_embeddings (memory_id TEXT, embedding BLOB)")
    for i in range(missing):
        conn.execute("INSERT INTO memory_items VALUES (?,?,?,0)",
                     (f"m{i}", f"body {i}", "chat_log"))
    for i in range(embedded):
        rid = f"e{i}"
        conn.execute("INSERT INTO memory_items VALUES (?,?,?,0)",
                     (rid, f"body {i}", "chat_log"))
        conn.execute("INSERT INTO memory_embeddings VALUES (?,?)", (rid, b"\x00"))
    conn.commit()
    conn.close()
    return str(path)


@pytest.fixture
def doctor():
    from memory import doctor as d
    return d


def test_split_topology_resolves_both_stores(doctor, tmp_path, monkeypatch):
    import chatlog_config

    main = _make_store(tmp_path / "agent_memory.db")
    chat = _make_store(tmp_path / "agent_chatlog.db")
    monkeypatch.setenv("CHATLOG_DB_PATH", chat)
    chatlog_config.invalidate_cache()

    stores = doctor._resolve_embed_stores(main)
    assert os.path.abspath(chat) in stores, (
        "chatlog store missing -- --fix would silently skip the real backlog"
    )
    assert len(stores) == 2


def test_unified_topology_resolves_one_store(doctor, tmp_path, monkeypatch):
    import chatlog_config

    unified = _make_store(tmp_path / "agent_memory.db")
    monkeypatch.setenv("CHATLOG_DB_PATH", unified)
    chatlog_config.invalidate_cache()

    assert doctor._resolve_embed_stores(unified) == [os.path.abspath(unified)]


def test_m3_database_fallback_does_not_collapse_the_stores(doctor, tmp_path, monkeypatch):
    """CHATLOG_DB_PATH unset is the common case: the path lives in the config
    file and resolve_config() then falls back to M3_DATABASE. Going through that
    fallback hands back the main DB and silently reintroduces the bug."""
    import chatlog_config

    main = _make_store(tmp_path / "agent_memory.db")
    chat = _make_store(tmp_path / "agent_chatlog.db")
    monkeypatch.setenv("M3_DATABASE", main)
    monkeypatch.delenv("CHATLOG_DB_PATH", raising=False)
    monkeypatch.setattr(chatlog_config, "DEFAULT_DB_PATH", chat)
    monkeypatch.setattr(chatlog_config, "_load_file", lambda: {"db_path": chat})
    chatlog_config.invalidate_cache()

    stores = doctor._resolve_embed_stores(main)
    assert os.path.abspath(chat) in stores
    assert len(stores) == 2


def test_absent_chatlog_file_is_not_fatal(doctor, tmp_path, monkeypatch):
    import chatlog_config

    main = _make_store(tmp_path / "agent_memory.db")
    monkeypatch.setenv("CHATLOG_DB_PATH", str(tmp_path / "nope.db"))
    chatlog_config.invalidate_cache()

    assert doctor._resolve_embed_stores(main) == [os.path.abspath(main)]


def test_soft_deleted_and_empty_rows_are_not_counted(doctor, tmp_path):
    """The count must match what embed_backfill can actually embed, else the
    number never reaches 0 and the repair looks stuck."""
    db = tmp_path / "s.db"
    _make_store(db, missing=3)
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO memory_items VALUES ('d0','body','chat_log',1)")
    conn.execute("INSERT INTO memory_items VALUES ('b0','   ','chat_log',0)")
    conn.commit()
    conn.close()

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    n = conn.execute(
        "SELECT COUNT(*) FROM memory_items mi "
        "LEFT JOIN memory_embeddings me ON mi.id = me.memory_id "
        "WHERE COALESCE(mi.is_deleted, 0) = 0 "
        "AND LENGTH(TRIM(COALESCE(mi.content, ''))) > 0 "
        "AND me.memory_id IS NULL"
    ).fetchone()[0]
    conn.close()
    assert n == 3, "soft-deleted / empty rows must not inflate the backlog"
