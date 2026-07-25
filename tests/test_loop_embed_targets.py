"""The embed pass must drain EVERY store, not just the main DB.

Regression guard for a split-topology hole: has_embed_work() and
run_embed_pass() both keyed off core_db alone, so on a deployment where the
chatlog lives in its own file the chat turns were NEVER embedded by the loop.
The gate kept reporting "no work" because the main DB was clean while the
chatlog backlog grew without bound (observed 2026-07-25: 6653 rows, FTS-
searchable but invisible to semantic search).
"""

import os
import sys

import pytest

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)


@pytest.fixture
def loop_mod():
    import m3_cognitive_loop
    return m3_cognitive_loop


def _touch_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return str(path)


def test_split_topology_targets_both_stores(loop_mod, tmp_path, monkeypatch):
    import chatlog_config

    main = _touch_db(tmp_path / "agent_memory.db")
    chat = _touch_db(tmp_path / "agent_chatlog.db")
    monkeypatch.setenv("CHATLOG_DB_PATH", chat)
    chatlog_config.invalidate_cache()

    targets = loop_mod._embed_target_dbs(main)
    assert os.path.abspath(main) in targets
    assert os.path.abspath(chat) in targets, (
        "chatlog store missing -- its backlog would never be embedded"
    )


def test_unified_topology_yields_one_target(loop_mod, tmp_path, monkeypatch):
    """When chatlog and main are the same file, sweep it once -- not twice."""
    import chatlog_config

    unified = _touch_db(tmp_path / "agent_memory.db")
    monkeypatch.setenv("CHATLOG_DB_PATH", unified)
    chatlog_config.invalidate_cache()

    targets = loop_mod._embed_target_dbs(unified)
    assert targets == [os.path.abspath(unified)]


def test_m3_database_override_does_not_mask_the_chatlog(loop_mod, tmp_path, monkeypatch):
    """The real-world split-topology failure, reproduced exactly.

    CHATLOG_DB_PATH is UNSET (the common case -- the path comes from the config
    file), and the loop sets M3_DATABASE. chatlog_config.resolve_config() then
    deliberately falls back to M3_DATABASE, handing back the MAIN db; the dedup
    collapses both entries to one and the chatlog is never swept. Resolution
    must read the config file directly rather than go through that fallback.

    Setting CHATLOG_DB_PATH here would mask the bug -- resolve_config() honors
    an explicit override, so the buggy code passes. It must stay unset.
    """
    import chatlog_config

    main = _touch_db(tmp_path / "agent_memory.db")
    chat = _touch_db(tmp_path / "agent_chatlog.db")
    monkeypatch.setenv("M3_DATABASE", main)
    monkeypatch.delenv("CHATLOG_DB_PATH", raising=False)
    # Point the config FILE at the chatlog store, as a real install does.
    monkeypatch.setattr(chatlog_config, "DEFAULT_DB_PATH", chat)
    monkeypatch.setattr(chatlog_config, "_load_file", lambda: {"db_path": chat})
    chatlog_config.invalidate_cache()

    targets = loop_mod._embed_target_dbs(main)
    assert os.path.abspath(chat) in targets, (
        "chatlog collapsed onto the main DB via the M3_DATABASE fallback -- "
        "its backlog would never be embedded"
    )
    assert len(targets) == 2


def test_missing_chatlog_db_is_not_fatal(loop_mod, tmp_path, monkeypatch):
    """A configured-but-absent chatlog file must not break the pass."""
    import chatlog_config

    main = _touch_db(tmp_path / "agent_memory.db")
    monkeypatch.setenv("CHATLOG_DB_PATH", str(tmp_path / "nope.db"))
    chatlog_config.invalidate_cache()

    targets = loop_mod._embed_target_dbs(main)
    assert targets == [os.path.abspath(main)]
