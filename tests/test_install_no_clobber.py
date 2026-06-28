"""Regression: install / upgrade must NOT clobber existing database files.

These guard the data-safety guarantee verified 2026-06-27:
  - `rmtree` only ever targets `repo_path` (a sibling of the engine root), so
    DBs in the decoupled `~/.m3/engine` layout are never in the removal path.
  - The legacy `repo/memory/*.db` preserve/restore path copies DBs out before
    the rmtree and back after — and uses the SQLite backup API (WAL-safe) for
    `.db` files.
  - The migration runner is idempotent, so a fresh install over a populated DB
    is a no-op on data.

Network/clone is mocked; nothing leaves the machine.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import os
import shutil
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _populate_db(path, table, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(path))
    c.execute(f"CREATE TABLE {table}(x TEXT)")
    c.execute(f"INSERT INTO {table} VALUES(?)", (value,))
    c.commit()
    c.close()


@pytest.fixture
def isolated_roots(tmp_path, monkeypatch):
    """Point the installer at an isolated decoupled-roots layout under tmp."""
    monkeypatch.setenv("M3_MEMORY_ROOT", str(tmp_path))
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setenv("M3_ENGINE_ROOT", str(tmp_path / "engine"))
    import m3_sdk
    importlib.reload(m3_sdk)
    # Re-import rather than reload when a sibling test (e.g. test_doctor's
    # module-purge fixture) has dropped these from sys.modules — importlib.reload
    # requires the module to still be registered, and raises "module ... not in
    # sys.modules" otherwise. import_module re-registers a fresh copy; reload
    # refreshes the existing one. Either way we get a copy that re-read the env
    # set above.
    if "m3_memory.installer" in sys.modules:
        from m3_memory import installer
        importlib.reload(installer)
    else:
        installer = importlib.import_module("m3_memory.installer")
    # Silence prompts / config writes; never hit the network.
    monkeypatch.setattr(installer, "_prompt_endpoint_choice", lambda *a, **k: None)
    monkeypatch.setattr(installer, "_prompt_capture_mode", lambda *a, **k: "none")
    monkeypatch.setattr(installer, "_prompt_cognitive_loop", lambda *a, **k: False)
    monkeypatch.setattr(installer, "save_config", lambda *a, **k: None)
    return m3_sdk, installer


def _fake_clone_minimal(dest):
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "bin").mkdir(parents=True, exist_ok=True)
    (dest / "bin" / "memory_bridge.py").write_text("# new payload")
    (dest / "memory").mkdir(exist_ok=True)
    return True


def test_upgrade_force_preserves_engine_dbs(isolated_roots, monkeypatch):
    m3_sdk, installer = isolated_roots
    eng = __import__("pathlib").Path(m3_sdk.get_m3_engine_root())

    mem = eng / "agent_memory.db"
    chat = eng / "agent_chatlog.db"
    files = eng / "files_database.db"
    _populate_db(mem, "memory_items", "precious-memory")
    _populate_db(chat, "chat", "captured-turns")
    _populate_db(files, "f", "indexed")
    before = {p: _sha(p) for p in (mem, chat, files)}

    # Existing repo -> triggers the --force rmtree path.
    repo = installer.default_repo_path()
    (repo / "bin").mkdir(parents=True, exist_ok=True)
    (repo / "bin" / "memory_bridge.py").write_text("# old")
    (repo / "memory").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(installer, "_git_clone", lambda tag, dest: _fake_clone_minimal(dest))

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            installer.install_m3(force=True, interactive=False, capture_mode="none")
        except Exception:
            pass  # post-fetch wiring may raise in the isolated env; data safety is what we assert

    for p, h in before.items():
        assert p.exists(), f"{p.name} was DELETED by install --force"
        assert _sha(p) == h, f"{p.name} was MODIFIED by install --force"


def test_fresh_install_no_op_on_populated_db(isolated_roots, monkeypatch):
    """No existing repo + a pre-existing populated DB: the real migration runner
    must be a no-op (DB byte-identical, data intact)."""
    m3_sdk, installer = isolated_roots
    eng = __import__("pathlib").Path(m3_sdk.get_m3_engine_root())
    mem = eng / "agent_memory.db"
    _populate_db(mem, "memory_items", "PRECIOUS")
    before = _sha(mem)

    assert not installer.default_repo_path().exists()  # genuinely fresh

    def fake_clone_full(tag, dest):
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(os.path.join(REPO_ROOT, "bin"), str(dest / "bin"), dirs_exist_ok=True)
        shutil.copytree(os.path.join(REPO_ROOT, "memory", "migrations"),
                        str(dest / "memory" / "migrations"), dirs_exist_ok=True)
        return True

    monkeypatch.setattr(installer, "_git_clone", fake_clone_full)

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            installer.install_m3(force=True, interactive=False, capture_mode="none")
        except Exception:
            pass

    assert mem.exists()
    assert _sha(mem) == before, "migration runner modified an up-to-date DB"
    cn = sqlite3.connect(str(mem))
    rows = cn.execute("SELECT x FROM memory_items").fetchall()
    cn.close()
    assert any(r[0] == "PRECIOUS" for r in rows), "user data lost"


def test_engine_root_never_under_repo(isolated_roots):
    """The structural guarantee: rmtree(repo) can't reach the engine DBs because
    engine is never a descendant of repo. Checked for the decoupled layout."""
    from pathlib import Path
    m3_sdk, installer = isolated_roots
    repo = installer.default_repo_path().resolve()
    eng = Path(m3_sdk.get_m3_engine_root()).resolve()
    with pytest.raises(ValueError):
        eng.relative_to(repo)  # raises if eng is NOT under repo == safe


def test_safe_copy_sqlite_wal_consistent(isolated_roots, tmp_path):
    """The legacy preserve copy uses the SQLite backup API: a DB with
    uncommitted-to-main WAL pages copies CONSISTENTLY (all committed rows present)."""
    _, installer = isolated_roots
    src = tmp_path / "live.db"
    # WAL mode + a committed write that lives in the -wal file.
    c = sqlite3.connect(str(src))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE t(x)")
    c.execute("INSERT INTO t VALUES('in-wal')")
    c.commit()
    # leave the connection OPEN (simulates the server holding the DB) so the WAL
    # isn't checkpointed into the main file.
    dst = tmp_path / "copied.db"
    installer._safe_copy_sqlite(src, dst)
    c.close()

    cn = sqlite3.connect(str(dst))
    rows = cn.execute("SELECT x FROM t").fetchall()
    cn.close()
    assert rows == [("in-wal",)], "backup-API copy missed a committed WAL row"


def test_safe_copy_sqlite_falls_back_for_non_db(isolated_roots, tmp_path):
    """A `.db` file that isn't valid SQLite (0-byte / garbage) is still preserved
    via a plain copy, not dropped."""
    _, installer = isolated_roots
    src = tmp_path / "empty.db"
    src.write_bytes(b"")  # 0-byte placeholder
    dst = tmp_path / "out.db"
    installer._safe_copy_sqlite(src, dst)
    assert dst.exists()
