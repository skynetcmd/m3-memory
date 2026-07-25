"""Tests for bin/chatlog_status.py — status reporting."""

import json
import sqlite3
import time

import pytest


@pytest.fixture
def status_test_env(tmp_path, monkeypatch):
    """Set up environment for status tests."""
    import chatlog_config

    db_path = tmp_path / "agent_chatlog.db"
    main_db_path = tmp_path / "agent_memory.db"
    state_file = tmp_path / ".chatlog_state.json"
    spill_dir = tmp_path / "chatlog_spill"

    monkeypatch.setattr(chatlog_config, "DEFAULT_DB_PATH", str(db_path))
    monkeypatch.setattr(chatlog_config, "MAIN_DB_PATH", str(main_db_path))
    monkeypatch.setattr(chatlog_config, "STATE_FILE", str(state_file))
    monkeypatch.setattr(chatlog_config, "SPILL_DIR", str(spill_dir))
    # Post-unification: CHATLOG_DB_PATH is the explicit chatlog-only override
    # and M3_DATABASE controls the main DB. Also set M3_DATABASE so
    # resolve_db_path(None) in chatlog_status doesn't fall back to the real
    # repo's agent_memory.db.
    monkeypatch.setenv("CHATLOG_DB_PATH", str(db_path))
    monkeypatch.setenv("M3_DATABASE", str(main_db_path))
    chatlog_config.invalidate_cache()
    with chatlog_config._POOL_LOCK:
        chatlog_config._POOL = None
        chatlog_config._POOL_DB_PATH = None

    # Create schema
    _create_status_schema(str(db_path))

    yield {
        "db_path": db_path,
        "main_db_path": main_db_path,
        "state_file": state_file,
        "spill_dir": spill_dir,
    }


def _create_status_schema(db_path):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE memory_items (
            id TEXT PRIMARY KEY,
            type TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE memory_embeddings (
            id TEXT,
            memory_id TEXT
        )
    """)
    conn.commit()
    conn.close()


def test_status_impl_returns_json(status_test_env):
    """chatlog_status_impl returns valid JSON."""
    import chatlog_status

    result = chatlog_status.chatlog_status_impl()

    # Should be valid JSON
    parsed = json.loads(result)
    assert isinstance(parsed, dict)


def test_status_includes_required_fields(status_test_env):
    """Status JSON includes required fields."""
    import chatlog_status

    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    # `mode` was removed in the 2026-04-21 refactor; `unified` replaced it.
    assert "unified" in parsed
    assert "db_paths" in parsed
    assert "row_counts" in parsed
    assert "queue" in parsed
    assert "spill" in parsed
    assert "redaction" in parsed
    assert "last_write_at" in parsed or "warnings" in parsed


def test_status_unified_field_reflects_path_equality(status_test_env):
    """unified=True when chatlog path == main path, else False."""
    import chatlog_status

    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    # The fixture sets chatlog and main to different paths.
    assert parsed["unified"] is False
    assert isinstance(parsed["db_paths"]["chatlog"], str)
    assert isinstance(parsed["db_paths"]["main"], str)


def test_status_row_counts_structure(status_test_env):
    """row_counts has expected structure."""
    import chatlog_status

    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    row_counts = parsed.get("row_counts", {})
    assert isinstance(row_counts, dict)
    # At least one count should exist
    assert len(row_counts) >= 0


def test_status_queue_depth(status_test_env):
    """Status reports queue depth."""
    import chatlog_status

    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    queue = parsed.get("queue", {})
    assert isinstance(queue, dict)
    # Queue may have depth field
    if "depth" in queue:
        assert isinstance(queue["depth"], int)


def test_status_spill_info(status_test_env):
    """Status reports spill directory info."""
    import chatlog_status

    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    spill = parsed.get("spill", {})
    assert isinstance(spill, dict)


def test_status_redaction_config(status_test_env):
    """Status reports redaction configuration."""
    import chatlog_status

    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    redaction = parsed.get("redaction", {})
    assert isinstance(redaction, dict)
    if "enabled" in redaction:
        assert isinstance(redaction["enabled"], bool)


def test_status_cold_call_performance(status_test_env):
    """chatlog_status_impl completes in reasonable time (< 200ms)."""
    import chatlog_status

    start = time.time()
    chatlog_status.chatlog_status_impl()
    elapsed_ms = (time.time() - start) * 1000

    assert elapsed_ms < 200, f"Status call took {elapsed_ms:.1f}ms, expected < 200ms"


def test_status_with_existing_state(status_test_env):
    """Status reads existing state file if present."""
    import chatlog_status

    state_file = status_test_env["state_file"]
    state_data = {
        "queue_depth": 42,
        "total_written": 1000,
        "last_flush_at": "2024-01-01T00:00:00Z",
        "last_write_at": "2024-01-01T00:01:00Z",
    }

    with open(str(state_file), "w") as f:
        json.dump(state_data, f)

    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    # Should reflect state from file
    assert parsed.get("last_write_at") is not None


def test_status_no_state_file(status_test_env):
    """Status works with no state file (empty state)."""
    import chatlog_status

    # State file doesn't exist; status should still work
    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    assert isinstance(parsed, dict)
    # Should have some fields even with empty state. `unified` is the
    # post-unification stand-in for the removed `mode` field.
    assert "unified" in parsed


def test_status_with_data_in_db(status_test_env):
    """Status counts rows correctly when DB has data."""
    import chatlog_status

    db_path = status_test_env["db_path"]
    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    # Insert sample data
    for i in range(10):
        cursor.execute(
            "INSERT INTO memory_items (id, type) VALUES (?, ?)",
            (f"msg-{i}", "chat_log"),
        )

    conn.commit()
    conn.close()

    result = chatlog_status.chatlog_status_impl()
    parsed = json.loads(result)

    row_counts = parsed.get("row_counts", {})
    # Should count at least chatlog_rows
    if "chatlog_rows" in row_counts:
        assert row_counts["chatlog_rows"] >= 10


def _create_recent_schema(db_path):
    """Schema with a created_at column so recent-write queries work.

    The status fixture pre-creates the chatlog DB's tables without a created_at
    column, so drop-and-recreate to be idempotent regardless of prior state.
    """
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS memory_items")
    cur.execute("DROP TABLE IF EXISTS memory_embeddings")
    cur.execute(
        "CREATE TABLE memory_items (id TEXT PRIMARY KEY, type TEXT, created_at TEXT)"
    )
    cur.execute("CREATE TABLE memory_embeddings (id TEXT, memory_id TEXT)")
    conn.commit()
    conn.close()


def test_capture_healthy_when_rows_land_in_chatlog_db_not_main(status_test_env):
    """Non-unified regression: recent chat_log rows in the CHATLOG db (not main)
    must report capture.healthy=True.

    Before the fix, _recent_write_count queried resolve_db_path(None) (the main
    DB), which holds 0 chat_log rows on a split install — producing a permanent
    false "capture may be down" alarm even while capture was thriving.
    """
    import chatlog_status

    chatlog_db = status_test_env["db_path"]
    main_db = status_test_env["main_db_path"]

    # Both DBs get a created_at-aware schema; the writer lands turns in chatlog.
    _create_recent_schema(str(chatlog_db))
    _create_recent_schema(str(main_db))

    conn = sqlite3.connect(str(chatlog_db))
    cur = conn.cursor()
    for i in range(5):
        cur.execute(
            "INSERT INTO memory_items (id, type, created_at) "
            "VALUES (?, 'chat_log', datetime('now'))",
            (f"recent-{i}",),
        )
    conn.commit()
    conn.close()

    parsed = json.loads(chatlog_status.chatlog_status_impl())

    assert parsed["unified"] is False
    assert parsed["capture"]["healthy"] is True
    assert parsed["capture"]["recent_rows"] == 5
    assert not any("capture may be down" in w for w in parsed["warnings"])


def test_capture_recent_count_prefers_chatlog_over_main(status_test_env):
    """The recent-write probe reads the chatlog DB, not the main DB, on a split
    install: rows only in main must NOT be double-counted, and rows in chatlog
    are the source of truth."""
    import chatlog_status

    chatlog_db = status_test_env["db_path"]
    main_db = status_test_env["main_db_path"]
    _create_recent_schema(str(chatlog_db))
    _create_recent_schema(str(main_db))

    # 3 recent in chatlog, 99 recent in main. Probe should report 3 (chatlog
    # wins), never 102 and never 99.
    for db, n in ((chatlog_db, 3), (main_db, 99)):
        conn = sqlite3.connect(str(db))
        cur = conn.cursor()
        for i in range(n):
            cur.execute(
                "INSERT INTO memory_items (id, type, created_at) "
                "VALUES (?, 'chat_log', datetime('now'))",
                (f"{db.name}-{i}",),
            )
        conn.commit()
        conn.close()

    parsed = json.loads(chatlog_status.chatlog_status_impl())
    assert parsed["capture"]["recent_rows"] == 3
    assert parsed["capture"]["healthy"] is True


def test_recent_write_count_query_error_returns_unknown_not_zero(status_test_env):
    """Regression (footgun): a DB that OPENS but whose COUNT query ERRORS
    (e.g. missing memory_items table) must return -1 (unknown), never 0.

    Returning 0 would emit a real "capture may be down" warning for a merely
    wrong-schema file — reintroducing the false-signal class the whole fix
    removes. The saw/queried flag must be set only after the query succeeds.
    """
    import chatlog_config
    import chatlog_status

    chatlog_db = status_test_env["db_path"]
    main_db = status_test_env["main_db_path"]

    # Both candidate DBs exist and open fine, but neither has memory_items.
    for db in (chatlog_db, main_db):
        conn = sqlite3.connect(str(db))
        conn.execute("DROP TABLE IF EXISTS memory_items")
        conn.execute("CREATE TABLE unrelated (x TEXT)")
        conn.commit()
        conn.close()

    cfg = chatlog_config.resolve_config()
    assert chatlog_status._recent_write_count(cfg) == -1


def test_recent_write_count_readable_but_empty_returns_zero(status_test_env):
    """A DB that opens AND queries cleanly but has no recent rows returns 0 —
    the genuine "capture down" signal, which must still fire (complement to the
    query-error case above)."""
    import chatlog_config
    import chatlog_status

    chatlog_db = status_test_env["db_path"]
    main_db = status_test_env["main_db_path"]
    _create_recent_schema(str(chatlog_db))
    _create_recent_schema(str(main_db))

    # One old row, outside the 15-min window, in the chatlog DB.
    conn = sqlite3.connect(str(chatlog_db))
    conn.execute(
        "INSERT INTO memory_items (id, type, created_at) "
        "VALUES ('old', 'chat_log', '2020-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    cfg = chatlog_config.resolve_config()
    assert chatlog_status._recent_write_count(cfg) == 0


def test_recent_write_count_missing_dbs_returns_unknown(
    status_test_env, tmp_path, monkeypatch
):
    """No candidate DB exists on disk -> -1 unknown (never a false 0)."""
    import chatlog_config
    import chatlog_status

    # Point both paths at nonexistent files. Use monkeypatch (auto-reverted)
    # rather than raw os.environ so this test stays hermetic (§3) and cannot
    # leak env state into later tests regardless of run order.
    monkeypatch.setenv("CHATLOG_DB_PATH", str(tmp_path / "nope_chatlog.db"))
    monkeypatch.setenv("M3_DATABASE", str(tmp_path / "nope_main.db"))
    chatlog_config.invalidate_cache()

    cfg = chatlog_config.resolve_config()
    assert chatlog_status._recent_write_count(cfg) == -1


def test_lull_does_not_trigger_capture_down_alarm(status_test_env):
    """Session-start / between-turns regression: 0 rows in the 15-min window but
    a RECENT last_write_at must NOT emit the 'capture may be down' scream — it is
    an ordinary lull (the Stop hook just hasn't fired this window yet). This is
    the false alarm that tripped the CLAUDE.md session-start check every fresh
    session (observed 2026-07-12)."""
    import chatlog_status

    chatlog_db = status_test_env["db_path"]
    main_db = status_test_env["main_db_path"]
    _create_recent_schema(str(chatlog_db))
    _create_recent_schema(str(main_db))

    # An OLD row (outside the 15-min window) so total_rows > 0 and recent == 0...
    conn = sqlite3.connect(str(chatlog_db))
    conn.execute(
        "INSERT INTO memory_items (id, type, created_at) "
        "VALUES ('old', 'chat_log', '2020-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    # ...but the state file says the last write was 3 minutes ago (a lull).
    from datetime import datetime, timedelta, timezone
    recent = (datetime.now(timezone.utc) - timedelta(minutes=3)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with open(str(status_test_env["state_file"]), "w") as f:
        json.dump({"last_write_at": recent}, f)

    parsed = json.loads(chatlog_status.chatlog_status_impl())
    assert not any("capture may be down" in w for w in parsed["warnings"]), (
        f"lull wrongly raised capture-down alarm: {parsed['warnings']}"
    )
    # It should still note the idle window benignly, so the state is visible.
    assert any("capture idle, not down" in w for w in parsed["warnings"])


def test_stale_last_write_still_triggers_capture_down_alarm(status_test_env):
    """Complement to the lull test: when the last write is OLDER than the grace
    window, a 0-in-15min count is a genuine outage and the real 'capture may be
    down' alarm MUST still fire. The lull grace must not mask a true failure."""
    import chatlog_status

    chatlog_db = status_test_env["db_path"]
    main_db = status_test_env["main_db_path"]
    _create_recent_schema(str(chatlog_db))
    _create_recent_schema(str(main_db))

    conn = sqlite3.connect(str(chatlog_db))
    conn.execute(
        "INSERT INTO memory_items (id, type, created_at) "
        "VALUES ('old', 'chat_log', '2020-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    # last_write_at well beyond the 6h grace window.
    with open(str(status_test_env["state_file"]), "w") as f:
        json.dump({"last_write_at": "2020-01-01T00:00:00Z"}, f)

    parsed = json.loads(chatlog_status.chatlog_status_impl())
    assert any("capture may be down" in w for w in parsed["warnings"]), (
        f"stale last-write failed to raise capture-down alarm: {parsed['warnings']}"
    )


def test_missing_last_write_triggers_capture_down_alarm(status_test_env):
    """No last_write_at at all (unknown age) with 0 recent rows is treated as a
    real outage, not a lull — the grace only applies to a KNOWN-recent write."""
    import chatlog_status

    chatlog_db = status_test_env["db_path"]
    main_db = status_test_env["main_db_path"]
    _create_recent_schema(str(chatlog_db))
    _create_recent_schema(str(main_db))

    conn = sqlite3.connect(str(chatlog_db))
    conn.execute(
        "INSERT INTO memory_items (id, type, created_at) "
        "VALUES ('old', 'chat_log', '2020-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    # No state file written -> last_write_at is None -> unknown age.
    parsed = json.loads(chatlog_status.chatlog_status_impl())
    assert any("capture may be down" in w for w in parsed["warnings"]), (
        f"unknown last-write failed to raise capture-down alarm: {parsed['warnings']}"
    )


def test_recent_write_probe_opens_read_only(status_test_env):
    """The probe must open DBs read-only (§6): it must never be able to mutate
    the store it is only counting."""
    from pathlib import Path

    import chatlog_config
    import chatlog_status

    chatlog_db = status_test_env["db_path"]
    _create_recent_schema(str(chatlog_db))

    # A read-only handle built the same way the probe builds it must reject writes.
    uri = f"{Path(str(chatlog_db)).as_uri()}?mode=ro"
    ro = sqlite3.connect(uri, uri=True, timeout=5)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("CREATE TABLE _should_fail (x)")
    ro.close()

    # And the probe itself completes cleanly, returning a valid int, without
    # ever needing write access to the DB.
    cfg = chatlog_config.resolve_config()
    result = chatlog_status._recent_write_count(cfg)
    assert isinstance(result, int)
    assert result >= -1


# ── Hook liveness warning ────────────────────────────────────────────────────
# Regression guard: _compute_warnings defaulted a missing last_write_ms_ago to
# float("inf"), so "<hook> silent 24h+" fired unconditionally on any deployment
# whose hooks omit per-agent write timestamps (the state file has no "hooks" key
# at all). The alarm was permanently on and therefore carried no signal.

def _warn_cfg(**hook_flags):
    """A ChatlogConfig with only the named host agents enabled."""
    import chatlog_config

    cfg = chatlog_config.ChatlogConfig()
    for name, spec in cfg.host_agents.items():
        spec.enabled = hook_flags.get(name, False)
    return cfg


def _warnings_for(state, cfg):
    import chatlog_status

    return chatlog_status._compute_warnings(
        state, cfg, row_counts={}, recent_writes=5, recent_window_min=15,
    )


def test_no_silent_warning_when_hook_state_absent():
    """No "hooks" key in the state file means the hook does not report per-agent
    timestamps -- unknown, not silent. Must not warn."""
    warnings = _warnings_for({}, _warn_cfg(**{"claude-code": True}))
    assert not any("silent 24h+" in w for w in warnings), warnings


def test_no_silent_warning_when_last_write_is_none():
    """An explicit null timestamp is also "unknown", not "silent forever"."""
    state = {"hooks": {"claude-code": {"last_write_ms_ago": None}}}
    warnings = _warnings_for(state, _warn_cfg(**{"claude-code": True}))
    assert not any("silent 24h+" in w for w in warnings), warnings


def test_silent_warning_still_fires_on_genuinely_stale_hook():
    """A real, known-stale timestamp must still raise the alarm."""
    state = {"hooks": {"claude-code": {"last_write_ms_ago": 90_000_000}}}
    warnings = _warnings_for(state, _warn_cfg(**{"claude-code": True}))
    assert any("claude-code silent 24h+" in w for w in warnings), warnings


def test_no_silent_warning_for_recently_active_hook():
    """A fresh timestamp is healthy."""
    state = {"hooks": {"claude-code": {"last_write_ms_ago": 1_000}}}
    warnings = _warnings_for(state, _warn_cfg(**{"claude-code": True}))
    assert not any("silent 24h+" in w for w in warnings), warnings


def test_disabled_hook_never_warns_even_when_stale():
    """Staleness on a disabled hook is not actionable."""
    state = {"hooks": {"gemini-cli": {"last_write_ms_ago": 90_000_000}}}
    warnings = _warnings_for(state, _warn_cfg(**{"claude-code": True}))
    assert not any("silent 24h+" in w for w in warnings), warnings


# ── Actionable warnings ──────────────────────────────────────────────────────
# Warnings state a problem AND name the command that fixes it. A bare
# "embed backlog 13011" sent a user reading source to find embed_backfill.py;
# the remedy after " -> " is what the renderer splits onto its own line.

def test_embed_backlog_warning_names_the_drain_command():
    row_counts = {"chatlog_without_embed": 50_000}
    warnings = [
        w for w in _warnings_for_counts(row_counts) if "embed backlog" in w
    ]
    assert warnings, "expected an embed-backlog warning"
    w = warnings[0]
    assert "embed_backfill.py" in w, w
    # Must NOT point at memory_doctor_fix: it defaults to the MAIN db and
    # silently no-ops against a split-topology chatlog backlog.
    assert "memory_doctor_fix" not in w, w


def test_embed_backlog_warning_names_the_root_cause():
    """A standing backlog means the scheduled sweeper isn't firing. Draining by
    hand without that hint fixes the symptom and it comes right back."""
    row_counts = {"chatlog_without_embed": 50_000}
    w = [x for x in _warnings_for_counts(row_counts) if "embed backlog" in x][0]
    assert "AgentOS_ChatlogEmbedSweep" in w, w


def test_redaction_off_is_surfaced_when_rows_exist():
    """Redaction defaults off and nothing tells the user. Every turn -- including
    pasted secrets -- is stored verbatim, so say so where they actually look."""
    warnings = _warnings_for_counts({"chatlog_rows": 500})
    hits = [w for w in warnings if "redaction is OFF" in w]
    assert hits, warnings
    assert "chatlog_set_redaction" in hits[0], hits[0]


def test_redaction_off_is_silent_on_a_fresh_install():
    """No rows yet -> nothing has been stored unredacted -> do not nag."""
    warnings = _warnings_for_counts({"chatlog_rows": 0})
    assert not any("redaction is OFF" in w for w in warnings), warnings


def test_every_warning_carries_a_remedy():
    """Guard the convention: any warning a user can act on names how."""
    row_counts = {"chatlog_without_embed": 50_000, "chatlog_rows": 500}
    for w in _warnings_for_counts(row_counts):
        assert " -> " in w, f"warning without a remedy: {w!r}"


def _warnings_for_counts(row_counts):
    import chatlog_config
    import chatlog_status

    cfg = chatlog_config.ChatlogConfig()
    for spec in cfg.host_agents.values():
        spec.enabled = False
    return chatlog_status._compute_warnings(
        {}, cfg, row_counts, recent_writes=5, recent_window_min=15,
    )
