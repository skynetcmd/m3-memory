"""pytest configuration and fixtures for chatlog + main-DB tests."""

# Add bin/ directory to Python path so tests can import bin modules
import os as _os

# Short-circuit cli._ensure_utf8's Windows UTF-8 re-exec for the whole test
# session. That helper runs at IMPORT time of m3_memory.cli (module-level call);
# on Windows, when the interpreter wasn't launched with -X utf8, it spawns a
# subprocess and sys.exit()s — which aborts any test that imports cli with a
# bare SystemExit (e.g. test_installer::test_auto_install_opt_out_via_env).
# Setting this sentinel makes _ensure_utf8 return immediately. Must be set
# BEFORE any test imports cli, so it lives at conftest import time.
_os.environ.setdefault("_M3_UTF8_REEXEC", "1")
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

_BIN_DIR = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)), "bin")
if _BIN_DIR not in sys.path:
    sys.path.insert(0, _BIN_DIR)


def embed_backend_reachable() -> bool:
    """True iff a tier-2 embedding backend answers a TCP connect quickly.

    Used to gate SLO / integration tests whose premise REQUIRES a reachable
    embedder (warm-cascade latency, tier-4 redaction roundtrip). On CI there is
    no embedder, so every cascade probe waits out its full retry/backoff and the
    sub-3s SLO can't hold — that's an environment limitation, not a regression.
    A fast 0.5s connect probe distinguishes the two without coupling the test to
    the network's timeout behavior.
    """
    import re
    import socket

    url = _os.environ.get("M3_EMBED_FALLBACK_URL", "http://127.0.0.1:8082")
    m = re.search(r"://([^:/]+):(\d+)", url)
    if not m:
        return False
    host, port = m.group(1), int(m.group(2))
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture(autouse=True)
def _restore_memory_modules():
    """Heal `sys.modules` pollution of the memory.* namespace after each test.

    Several tests deliberately purge / reimport `memory_core` and `memory.*`
    (e.g. test_doctor's _isolate_env, the shim-identity test, the parity tests)
    to exercise a fresh import under a specific env. When they don't restore
    afterward, the rest of the session runs against a half-initialized or
    re-initialized module set — config globals revert to defaults
    (ELBOW_MIN_INPUT 5->20), monkeypatches target a stale `config`, and
    `import memory_core` returns a broken cached module. That surfaced as
    order-dependent CI reds that pass in isolation: test_oxidation_probe,
    test_elbow_trim, test_entity_coalesce, test_memory_core_parity, etc.

    Snapshot the memory-namespace modules before the test; after it, drop any
    that were added/replaced and restore the originals. Tests that intentionally
    reimport still see their fresh module DURING the test; the next test starts
    from the original, fully-initialized objects.
    """
    def _snapshot():
        return {
            name: mod for name, mod in sys.modules.items()
            if name == "memory_core" or name == "memory" or name.startswith("memory.")
        }

    before = _snapshot()
    yield
    after_names = {
        name for name in sys.modules
        if name == "memory_core" or name == "memory" or name.startswith("memory.")
    }
    # Remove modules the test introduced/replaced, then restore the originals.
    for name in after_names - before.keys():
        del sys.modules[name]
    for name, mod in before.items():
        sys.modules[name] = mod


# Minimal memory_items schema sufficient for chatlog writes. Embeddings and
# FTS5 tables are created lazily by specific fixtures that need them.
#
# Kept as a small chatlog-side helper. Main-DB tests should use
# `create_full_main_schema` (post-v031 canonical schema, built from the
# session-scoped template DB).
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
    # CHATLOG_MODE is deprecated / ignored. Scope chatlog + main to tmp so a
    # test run never touches the live store.
    monkeypatch.delenv("CHATLOG_MODE", raising=False)
    monkeypatch.setenv("CHATLOG_DB_PATH", str(db_path))
    monkeypatch.setenv("M3_DATABASE", str(main_db_path))
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
    """Create the minimal memory_items table. Safe to call on an existing DB.

    For full main-DB schema use `create_full_main_schema` instead.
    """
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_MEMORY_ITEMS_SCHEMA)
    conn.commit()
    conn.close()


# ──────────────────────────────────────────────────────────────────────────────
# Full main-DB schema fixture — pinned to current migrations
# ──────────────────────────────────────────────────────────────────────────────
# Tests that need the post-v031 canonical schema (memory_items + 30+ feature
# tables, entity graph, fact_enrichment_queue, observation_queue, etc.) should
# call `create_full_main_schema(db_path)` instead of inlining tiny synthetic
# schemas. This way, when migrations evolve (v032, v033, ...) the test fixtures
# track automatically — no manual schema duplication.
#
# Implementation: a session-scoped template DB is built ONCE per pytest run by
# invoking `bin/migrate_memory.py up --yes --target main` against a fresh DB.
# Per-test copies are bytewise file-copies — fast (~ms) instead of paying the
# subprocess + 31-migration cost per test.

_TEMPLATE_DB_PATH: Path | None = None


def _build_template_db() -> Path:
    """Run all main migrations against a fresh DB and return its path.

    Cached at module level — built once per pytest session. The template
    is created in tempdir (so the session's tearDown cleans it up).
    """
    tmp_root = Path(tempfile.mkdtemp(prefix="m3_test_template_"))
    template_db = tmp_root / "template.db"

    repo_root = Path(__file__).resolve().parent.parent
    migrate_script = repo_root / "bin" / "migrate_memory.py"

    env = _os.environ.copy()
    env["M3_DATABASE"] = str(template_db)
    # The migrator's backup step writes to ~/.m3-memory/backups by default;
    # redirect to tmp so we don't pollute the real backup directory.
    env["M3_BACKUP_DIR"] = str(tmp_root / "backups")

    result = subprocess.run(
        [sys.executable, str(migrate_script), "up", "--yes", "--target", "main"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"failed to build test template DB:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    if not template_db.is_file():
        raise RuntimeError(f"template DB not created at {template_db}")
    return template_db


def _get_template_db() -> Path:
    global _TEMPLATE_DB_PATH
    if _TEMPLATE_DB_PATH is None or not _TEMPLATE_DB_PATH.is_file():
        _TEMPLATE_DB_PATH = _build_template_db()
    return _TEMPLATE_DB_PATH


def create_full_main_schema(db_path) -> None:
    """Create a fresh main-DB-schema at `db_path` (post-v031 canonical).

    Implementation: copies a session-cached template DB built by running
    all migrations once. Fast (~ms per test) and always in sync with the
    current migration files — no schema duplication to maintain.

    Use this in place of inlined `CREATE TABLE memory_items (...)` blocks
    in tests that exercise main-DB code paths.
    """
    template = _get_template_db()
    shutil.copyfile(str(template), str(db_path))


@pytest.fixture(scope="session")
def main_db_template() -> Path:
    """Session-scoped: returns the path to a fresh post-v031 main DB.

    Tests that copy this directly should call `shutil.copyfile(template, dst)`.
    Most callers should use `create_full_main_schema(db_path)` instead, which
    handles the copy and is the public API.
    """
    return _get_template_db()
