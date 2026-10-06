

def test_ingest_cursor_lives_under_the_engine_root_and_reads_the_old_copy(tmp_path, monkeypatch):
    """The cursor moved out of the payload (a reinstall wipes the payload). The
    first load after the move still reads the old copy so progress is kept."""
    import json

    import chatlog_config
    import chatlog_ingest

    new = tmp_path / "engine" / ".chatlog_ingest_cursor.json"
    old = tmp_path / "payload" / "memory" / ".chatlog_ingest_cursor.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"seen": 1}), encoding="utf-8")
    monkeypatch.setattr(chatlog_config, "INGEST_CURSOR", new)
    monkeypatch.setattr(chatlog_ingest, "_legacy_cursor_path", lambda: str(old))

    assert chatlog_ingest._cursor_path() == str(new)
    assert chatlog_ingest._load_cursor() == {"seen": 1}
    chatlog_ingest._save_cursor({"seen": 2})
    assert json.loads(new.read_text(encoding="utf-8")) == {"seen": 2}
