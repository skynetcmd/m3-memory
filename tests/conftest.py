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

# Disable the native m3_core_rs extension by DEFAULT for the whole test session.
# Unit tests should not depend on a CUDA/native wheel being installed on the
# host. On a dev box that HAS the wheel + a discoverable GGUF, any test that
# exercises an embed would otherwise load the real in-process EmbeddedEmbedder
# — a multi-second CUDA context + model load that (a) blows the doctor <5s SLO
# assertions and (b) leaves warm global embed state that poisons run-order-
# dependent tier-ordering / dedup / cold-cascade tests (they pass in isolation,
# fail under batch ordering). Tests that specifically exercise the native path
# opt back in explicitly (e.g. test_sdk_oxidation / test_governor_pacing set
# M3_CORE_RS_DISABLE=0 / monkeypatch the flag). setdefault so an outer
# environment that deliberately set it wins. Must run at conftest IMPORT time,
# before any test imports memory.config (which reads it once at import).
_os.environ.setdefault("M3_CORE_RS_DISABLE", "1")
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
    after = {
        name: sys.modules[name] for name in list(sys.modules)
        if name == "memory_core" or name == "memory" or name.startswith("memory.")
    }
    # Detect whether the test REPLACED any memory.* module object (not just
    # added one). A replacement means some submodule was reimported and may now
    # be bound, via `from .x import y`, to a DIFFERENT sibling than the one we'd
    # restore — leaving a divergent mix where e.g. memory.entity's cached
    # `_embed_canonical_cached` reads a different memory.embed cache than a test
    # patches. That cross-module identity divergence is what made
    # test_entity_graph::test_resolution_cosine fail only under batch ordering
    # (the stub _embed was patched on one memory.embed instance but the
    # resolution path called another). When a replacement happened, restoring
    # the old objects can't guarantee internal consistency — so PURGE the whole
    # memory.* namespace instead and let the next test reimport a clean,
    # consistently cross-bound set. When nothing was replaced (the common case),
    # keep the cheap restore.
    replaced = any(
        name in before and before[name] is not mod
        for name, mod in after.items()
    )
    if replaced:
        for name in list(sys.modules):
            if name == "memory_core" or name == "memory" or name.startswith("memory."):
                del sys.modules[name]
        return
    # No replacement: drop only what the test ADDED, restore the originals.
    for name in set(after) - set(before):
        del sys.modules[name]
    for name, mod in before.items():
        sys.modules[name] = mod


@pytest.fixture(autouse=True)
def _guard_thread_leaks():
    """Fail fast when a test leaks a live worker thread into the session.

    A test that spawns a background thread which never exits (e.g. a simulated
    wedged native init, `threading.Event().wait()` with no release) leaves that
    thread alive for the rest of the run. Beyond hiding a real bug, a lingering
    NATIVE-touching thread can race a later test that reloads a C-extension
    module and crash the whole run (that is exactly what happened with the
    m3-embed-init thread and the crypto_provider reload, #85).

    Guard: snapshot the live threads before the test; after it, any NEW thread
    still alive after a short grace is a leak. Threads whose name matches a
    known worker prefix (they should be joined/stopped by their test) FAIL the
    test; anything else is surfaced as a warning so genuinely long-lived helpers
    don't turn into false reds. Give stragglers a brief join first — a thread
    mid-shutdown at yield time is not a leak.
    """
    import threading
    import warnings

    # Worker-thread name prefixes that a well-behaved test must not leave running.
    _LEAK_FAIL_PREFIXES = ("m3-embed-init",)

    before = {t.ident for t in threading.enumerate()}
    yield
    current = threading.current_thread()

    def _new_live():
        return [
            t for t in threading.enumerate()
            if t.ident not in before and t is not current and t.is_alive()
        ]

    # A leaked thread may be a few ms from exiting; give it a short window.
    for t in _new_live():
        t.join(timeout=2.0)
    leaked = _new_live()
    if not leaked:
        return
    fail = [t for t in leaked if t.name.startswith(_LEAK_FAIL_PREFIXES)]
    if fail:
        names = ", ".join(sorted(t.name for t in fail))
        raise AssertionError(
            f"test leaked live worker thread(s): {names}. Join/stop them in "
            "teardown (an unreleased native-init thread can crash a later "
            "module reload — see #85)."
        )
    # Non-worker stragglers: surface but don't fail (may be a legit helper).
    warnings.warn(
        "test left non-main thread(s) alive: "
        + ", ".join(sorted(t.name for t in leaked)),
        stacklevel=2,
    )


@pytest.fixture(autouse=True)
def _close_db_pools():
    """Close every cached M3Context SQLite pool after each test.

    M3Context.for_db() caches contexts (each owning a 5-connection pool) in a
    16-entry LRU and NEVER closes them between tests — so pooled connections to
    every test's tmp DB accumulate, holding open file handles + WAL locks. Under
    CI's slower I/O that cross-test contention surfaces as OperationalError:
    database is locked (m3_sdk.py:494) in test_entity_* / resolution-tuning,
    which passed locally only because fast local I/O dodged the race. WAL mode
    alone wasn't enough — the leaked pooled connections are the real cause.

    m3_sdk._cleanup() closes all pools and clears the cache (it's the same
    routine registered atexit). Run it as teardown so each test starts with no
    inherited open connections. Best-effort: a missing/funny m3_sdk must not
    break unrelated tests.
    """
    yield
    try:
        import m3_sdk
        m3_sdk._cleanup()
    except Exception:  # noqa: BLE001 — teardown hygiene must never fail a test
        pass


@pytest.fixture(autouse=True)
def _reset_storage_backend_cache(monkeypatch):
    """Reset the storage-backend selector cache + backend env around every test.

    `memory.backends.selector` caches the resolved StorageBackend per name in a
    process-global dict (so pools/capability probes aren't rebuilt every call).
    A test that resolves `active_backend()` under `M3_DB_BACKEND=postgres` (the
    seam unit tests, and every live-PG test) leaves the postgres backend cached;
    a LATER test on a different file that expects the sqlite default then gets the
    stale cached postgres backend and fails — an order-dependent cross-file leak.

    Clearing the cache before AND after each test, plus forcing the env back to
    the sqlite default, makes every test start from a clean, deterministic
    backend regardless of run order. Best-effort: the selector must import, but a
    funny state must never break an unrelated test.
    """
    def _clear():
        try:
            from memory.backends import selector as _sel
            _sel._reset_for_tests()
        except Exception:  # noqa: BLE001 — hygiene must never fail a test
            pass

    # Force the default backend + a clean DB env unless a test explicitly opts
    # in. Clearing M3_PG_URL/PG_URL too keeps an ambient dev DSN (a developer
    # running the suite with M3_PG_URL exported to a throwaway cluster) from
    # leaking into tests that read it — e.g. test_backup_reminder's
    # _detect_cdw_target, which sets the lower-precedence PG_URL but is overridden
    # by a leaked higher-precedence M3_PG_URL. A test that needs these sets them
    # via monkeypatch, which runs after this fixture and wins.
    monkeypatch.delenv("M3_DB_BACKEND", raising=False)
    monkeypatch.delenv("DB_BACKEND", raising=False)
    monkeypatch.delenv("M3_PG_URL", raising=False)
    monkeypatch.delenv("PG_URL", raising=False)
    _clear()
    yield
    _clear()


@pytest.fixture(autouse=True)
def _reset_embed_global_cache():
    """Reset memory.embed's in-process embedder cache between tests.

    `memory.embed` memoizes the in-process EmbeddedEmbedder in module-level
    globals (`_embedded_embedder`, `_embedded_embed_checked`) and only probes
    once per process (the `_embedded_embed_checked` guard). On a host with the
    native m3_core_rs wheel installed AND a discoverable GGUF, the FIRST test
    that exercises an embed warms that cache — and `_restore_memory_modules`
    above restores the module IDENTITY but NOT its mutated internal globals. So
    every later test inherits a live tier-1 embedder, which silently satisfies
    embeds before the HTTP cascade is reached. That made tier-ordering / dedup /
    cold-cascade tests (test_embed_cascade_order, test_vector_kind_strategy,
    test_doctor SLOs) pass in isolation but fail under batch ordering on dev
    boxes with the wheel installed.

    Clearing the cache to its pristine "not yet checked" state after each test
    means the next test re-probes under ITS OWN env (e.g. M3_CORE_RS_DISABLE /
    M3_EMBED_GGUF), so tier-1 presence is decided per-test, not leaked.
    Best-effort: if memory.embed isn't imported / lacks the globals, do nothing.
    """
    yield
    mod = sys.modules.get("memory.embed")
    if mod is not None:
        if hasattr(mod, "_embedded_embedder"):
            mod._embedded_embedder = None
        if hasattr(mod, "_embedded_embed_checked"):
            mod._embedded_embed_checked = False
        # The entity-name embed cache memoizes name->vector across calls. A
        # sibling test that resolves entities leaves it populated; a later test
        # that stubs _embed and asserts on embed_calls then sees ZERO calls
        # because the resolution path served the name from this cache instead
        # (test_entity_graph::test_resolution_cosine_stores...). Clear it too.
        cache = getattr(mod, "_ENTITY_NAME_EMBED_CACHE", None)
        if cache is not None:
            try:
                cache.clear()
            except (AttributeError, TypeError):
                pass


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
    # Put the copy in WAL mode (production default) so the m3_sdk pool's 5
    # connections and any raw test connection coexist without an exclusive-lock
    # fight. Without this, under CI's slower I/O the pool's WAL-mode switch
    # contends with a test reader and fails with "database is locked"
    # (m3_sdk.py:494) — the cause of flaky test_entity_* / resolution-tuning reds.
    _wal = sqlite3.connect(str(db_path), timeout=30)
    try:
        _wal.execute("PRAGMA journal_mode=WAL")
        _wal.commit()
    finally:
        _wal.close()


@pytest.fixture(scope="session")
def main_db_template() -> Path:
    """Session-scoped: returns the path to a fresh post-v031 main DB.

    Tests that copy this directly should call `shutil.copyfile(template, dst)`.
    Most callers should use `create_full_main_schema(db_path)` instead, which
    handles the copy and is the public API.
    """
    return _get_template_db()
