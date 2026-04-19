"""pytest configuration and fixtures for chatlog tests."""

import sys
import os
import sqlite3

# Add bin/ directory to Python path so tests can import bin modules
bin_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "bin")
if bin_dir not in sys.path:
    sys.path.insert(0, bin_dir)


# Minimal memory_items schema sufficient for chatlog writes. Embeddings and
# FTS5 tables are created lazily by specific fixtures that need them.
_MEMORY_ITEMS_SCHEMA = """
    CREATE TABLE IF NOT EXISTS memory_items (
        id TEXT PRIMARY KEY,
        type TEXT,
        title TEXT,
        content TEXT,
        metadata_json TEXT,
        agent_id TEXT,
        model_id TEXT,
        change_agent TEXT,
        importance REAL,
        source TEXT,
        origin_device TEXT,
        user_id TEXT,
        scope TEXT,
        expires_at TEXT,
        created_at TEXT,
        valid_from TEXT,
        valid_to TEXT,
        conversation_id TEXT,
        refresh_on TEXT,
        refresh_reason TEXT,
        content_hash TEXT,
        variant TEXT
    );
"""


def isolate_chatlog_env(monkeypatch, tmp_path):
    """Route every chatlog-subsystem side effect into tmp_path.

    This centralises the three-layer isolation that chatlog fixtures need:

    1. **Config paths** — monkeypatch the module-level path constants so tools
       that read them directly (status line, init, migrate) see tmp paths.
    2. **CHATLOG_DB_PATH env var** — the dataclass default `db_path` captured
       `DEFAULT_DB_PATH` at class-definition time, so patching the constant
       alone does *not* steer new configs. The env-var override is the only
       reliable knob for `resolve_config()`.
    3. **Module globals** — `chatlog_config._POOL` / `_POOL_DB_PATH` and
       `chatlog_core._QUEUE` / `_FLUSH_TASK` are kept alive across tests by
       the import cache. Without explicit teardown, a stale pool pointing at
       the real DB (or a queue from a prior test's flush) will leak writes.

    Returns a dict of the tmp paths for tests that need to open the DB or
    inspect spill files directly.
    """
    import chatlog_config
    import chatlog_core

    db_path = tmp_path / "agent_chatlog.db"
    main_db_path = tmp_path / "agent_memory.db"
    state_file = tmp_path / ".chatlog_state.json"
    spill_dir = tmp_path / "chatlog_spill"

    monkeypatch.setattr(chatlog_config, "DEFAULT_DB_PATH", str(db_path))
    monkeypatch.setattr(chatlog_config, "MAIN_DB_PATH", str(main_db_path))
    monkeypatch.setattr(chatlog_config, "STATE_FILE", str(state_file))
    monkeypatch.setattr(chatlog_config, "SPILL_DIR", str(spill_dir))
    monkeypatch.setenv("CHATLOG_MODE", "separate")
    monkeypatch.setenv("CHATLOG_DB_PATH", str(db_path))
    chatlog_config.invalidate_cache()

    monkeypatch.setattr(chatlog_config, "_POOL", None)
    monkeypatch.setattr(chatlog_config, "_POOL_DB_PATH", None)
    monkeypatch.setattr(chatlog_core, "_QUEUE", None)
    monkeypatch.setattr(chatlog_core, "_FLUSH_TASK", None)

    return {
        "db_path": db_path,
        "main_db_path": main_db_path,
        "state_file": state_file,
        "spill_dir": spill_dir,
    }


def create_memory_items_schema(db_path) -> None:
    """Create the memory_items table. Safe to call on an existing DB."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_MEMORY_ITEMS_SCHEMA)
    conn.commit()
    conn.close()
