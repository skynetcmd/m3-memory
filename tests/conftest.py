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


# ──────────────────────────────────────────────────────────────────────────────
# Capability probes + gating (docs/design/TEST_SUITE_DESIGN.md)
# ──────────────────────────────────────────────────────────────────────────────
# ONE probe per external capability, and ONE marker per capability. A test that
# needs a resource carries the `requires_*` marker; pytest_collection_modifyitems
# auto-skips it when the probe reports the resource absent — so no test file
# re-derives DSNs, reachability checks, or skipif conditions. This replaces the
# five hand-rolled gating idioms catalogued in the design doc and fixes their
# latent inconsistencies (a typo'd PG skip reason, presence-vs-reachability drift,
# two GGUF env vars, three native-wheel idioms).


def pg_dsn() -> "str | None":
    """The Postgres DSN for live tests — the SINGLE source of the precedence rule.

    M3_PRIMARY_PG_URL > M3_PG_URL, and NEVER PG_URL (PG_URL points at PROD; see
    CLAUDE.md and the pg-url-split memory). Centralizing it here means the 12
    former copies can't drift (one had already diverged to name M3_PG_URL in its
    skip reason).
    """
    return (_os.environ.get("M3_PRIMARY_PG_URL")
            or _os.environ.get("M3_PG_URL") or "").strip() or None


def _pg_reachable() -> bool:
    """True iff a Postgres cluster answers within a short connect timeout.
    Reachability, not mere presence — the presence-only gate in the old
    test_backend_conformance was an inconsistency this unifies away."""
    dsn = pg_dsn()
    if not dsn:
        return False
    try:
        import psycopg2
        psycopg2.connect(dsn, connect_timeout=3).close()
        return True
    except Exception:  # noqa: BLE001 — any failure = not reachable
        return False


def _native_wheel_present() -> bool:
    """True iff the m3_core_rs native extension is importable (not the
    pure-Python fallback). Unifies the three former idioms (importorskip /
    in-body skip / _has_native_governor)."""
    import importlib.util
    return importlib.util.find_spec("m3_core_rs") is not None


def gguf_path() -> "str | None":
    """The GGUF model path for tests that need a real model — via the SINGLE
    canonical env var M3_TEST_GGUF (the old suite split between M3_TEST_GGUF and
    M3_EMBED_GGUF for the same purpose)."""
    return (_os.environ.get("M3_TEST_GGUF") or "").strip() or None


_FILES_DB = Path(__file__).resolve().parent.parent / "memory" / "files_database.db"


def _files_db_present() -> bool:
    return _FILES_DB.is_file()


# Marker -> probe. A marked test is skipped (with the given reason) when its
# probe returns False. One place; adding a capability is one row.
_CAPABILITY_PROBES = {
    "requires_pg": (_pg_reachable,
                    "no reachable PostgreSQL (set M3_PRIMARY_PG_URL to a throwaway cluster)"),
    "requires_embedder": (embed_backend_reachable,
                          "no reachable embedder (M3_EMBED_FALLBACK_URL, default :8082)"),
    "requires_native": (_native_wheel_present,
                        "m3_core_rs native wheel not installed"),
    "requires_gguf": (lambda: gguf_path() is not None,
                      "no GGUF model (set M3_TEST_GGUF)"),
    "requires_files_db": (_files_db_present,
                          "shipped files_database.db absent"),
}


def pytest_collection_modifyitems(config, items):
    """Auto-skip capability-marked tests whose resource is absent, and give every
    requires_* test the `integration` umbrella marker so `-m "not integration"`
    selects the hermetic lane in one expression. Probes are memoized per session
    so a marker used by 50 tests probes the cluster once, not 50 times."""
    cache: "dict[str, bool]" = {}

    def _ok(marker: str) -> bool:
        if marker not in cache:
            cache[marker] = bool(_CAPABILITY_PROBES[marker][0]())
        return cache[marker]

    for item in items:
        marks = set(item.keywords)
        capability_marks = [m for m in _CAPABILITY_PROBES if m in marks]
        if capability_marks:
            item.add_marker(pytest.mark.integration)
        for m in capability_marks:
            if not _ok(m):
                item.add_marker(pytest.mark.skip(reason=_CAPABILITY_PROBES[m][1]))


@pytest.fixture
def pg_url() -> str:
    """DSN for a reachable Postgres. Requesting this fixture in a `requires_pg`
    test guarantees (via the collection hook) the cluster is up, so this just
    returns it. Kept as a fixture so test bodies get the value without importing
    the probe."""
    dsn = pg_dsn()
    if not dsn or not _pg_reachable():
        pytest.skip("no reachable PostgreSQL (set M3_PRIMARY_PG_URL to a throwaway cluster)")
    return dsn


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
def _reset_storage_backend_cache():
    """Reset the storage-backend selector cache around every test.

    `memory.backends.selector` caches the resolved StorageBackend per name in a
    process-global dict (so pools/capability probes aren't rebuilt every call).
    A test that resolves `active_backend()` under `M3_DB_BACKEND=postgres` (the
    seam unit tests, and every live-PG test) leaves the postgres backend cached;
    a LATER test on a different file that expects the sqlite default then gets the
    stale cached postgres backend and fails — an order-dependent cross-file leak.

    This resets the CACHE only. The backend-selecting ENV (M3_DB_BACKEND, PG
    URLs, CDW target) is cleared by the `m3_sandbox` fixture, which owns all
    environment isolation in one place. Best-effort: the selector must import,
    but a funny state must never break an unrelated test.
    """
    def _clear():
        # IMPORTANT: only clear if the selector is ALREADY imported — do NOT
        # trigger the import here. Importing memory.backends eagerly pulls the
        # whole memory.* package (memory/__init__ imports all submodules incl.
        # memory.embed in the host's shared-embedder mode). Doing that during
        # this autouse fixture's setup polluted the module snapshot that
        # _restore_memory_modules takes, so test_doctor's tier-1 probe read a
        # shared-mode config and reported 'shared-mode' instead of
        # 'not-configured' (regression from the test-isolation commit). Guarding
        # on sys.modules keeps this fixture a no-op for tests that never touch the
        # backend seam.
        _sel = sys.modules.get("memory.backends.selector")
        if _sel is not None:
            try:
                _sel._reset_for_tests()
            except Exception:  # noqa: BLE001 — hygiene must never fail a test
                pass

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


# Environment variables that must NOT leak from the developer's shell into a
# test. Grouped by concern so the single source of truth is legible. A test that
# genuinely needs one sets it via monkeypatch, which runs AFTER this autouse
# fixture and therefore wins.
_SANDBOX_CLEAR_ENV = (
    # Backend selector — a leaked M3_DB_BACKEND=postgres makes sqlite-default
    # tests resolve the wrong backend.
    "M3_DB_BACKEND", "DB_BACKEND",
    # Primary-store DSNs — an ambient dev DSN to a throwaway cluster leaks into
    # tests that read PG_URL (higher-precedence M3_PG_URL overrides an injected
    # PG_URL otherwise).
    "M3_PG_URL", "PG_URL",
    # CDW / warehouse-sync target — a real M3_SYNC_TARGET_IP=10.x / M3_CDW_PG_URL
    # overrides a test's injected target via getenv_compat precedence (that made
    # test_backup_reminder assert on 192.0.2.50 but read the real host).
    "M3_SYNC_TARGET_IP", "SYNC_TARGET_IP",
    "M3_CDW_PG_URL", "M3_CDW_URL",
    "M3_POSTGRES_SERVER", "POSTGRES_SERVER",
)


@pytest.fixture(autouse=True)
def m3_sandbox(monkeypatch, tmp_path):
    """The single source of truth for a hermetic test environment.

    Every test runs inside a filesystem + environment sandbox so it neither
    reads the developer's real config/warehouse nor writes into their real
    engine/backup dirs. This ONE fixture replaces the previously scattered
    env-isolation (a dedicated engine-root fixture + the env half of the
    backend-cache fixture + ~130 lines of per-file `monkeypatch.setenv` for
    M3_*_ROOT / PG_URL / M3_DATABASE). Consolidating it here is what closes the
    "gap between fixtures" leak class — the backup-dir leak and the CDW-env leak
    both existed because isolation was piecemeal and each covered only part of
    the surface.

    Two halves, both backend-agnostic (SQLite / PostgreSQL / future backends):

    1. **Roots → tmp.** Pin all three M3 roots to per-test tmp dirs at their
       HIGHEST-precedence env vars:
         * ``M3_ENGINE_ROOT``  — DBs, engine state, and (crucially) the migrator's
           ``get_m3_engine_root()/backups`` pre-upgrade backups. A test that ran a
           migration previously leaked ``*.pre-up.*.db`` into the real backup dir
           (GBs of ``test_agent_chatlog`` backups over time).
         * ``M3_CONFIG_ROOT``  — load-bearing for the SUBPROCESS leak path:
           ``memory.db._ensure_sync_tables`` shells out to ``migrate_memory.py``
           with ``os.environ.copy()``; that subprocess's ``prompt_backup_dir``
           prefers the SAVED ``backup_dir`` in ``.migrate_config.json``. On a dev
           box that saved value points at the real dir, and a bare
           ``M3_MEMORY_ROOT`` does not override an already-exported real
           ``M3_CONFIG_ROOT`` — so only pinning the config root at top precedence
           makes both in-process and subprocess read a clean, empty config.
         * ``M3_MEMORY_ROOT``  — the master root others derive from when unset.
       Also re-points ``migrate_memory``'s import-time module constants
       (``_M3_ENGINE_ROOT`` / ``CONFIG_PATH``) if it's already imported, so a
       pre-imported module doesn't retain real-root values.
       tmp_path is unique per test and reaped by pytest, so anything written under
       it is cleaned up automatically — tests clean up after themselves.

    2. **Leaky env → cleared.** Remove the backend / PG / CDW-sync vars listed in
       ``_SANDBOX_CLEAR_ENV`` so a developer running the suite with a real
       warehouse exported can't have that host bleed into assertions.
    """
    # 1. Roots → tmp (highest-precedence env vars).
    monkeypatch.setenv("M3_ENGINE_ROOT", str(tmp_path / "engine"))
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setenv("M3_MEMORY_ROOT", str(tmp_path))
    # Pinning M3_MEMORY_ROOT to tmp also blinds discovery of SHIPPED read-only
    # payload that derives from <M3_MEMORY_ROOT>/config/ — notably the SLM profiles
    # (bin/slm_intent._profile_search_dirs → <root>/config/slm). Those are part of
    # the repo, not per-test state, so point M3_SLM_PROFILES_DIR at the real repo
    # profiles dir; tests that manage their own profiles (test_slm_intent) set this
    # var themselves and override us (monkeypatch runs after this autouse fixture).
    _repo_slm = Path(__file__).resolve().parent.parent / "config" / "slm"
    if _repo_slm.is_dir():
        monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(_repo_slm))
    _mm = sys.modules.get("migrate_memory")
    if _mm is not None:
        if hasattr(_mm, "_M3_ENGINE_ROOT"):
            monkeypatch.setattr(_mm, "_M3_ENGINE_ROOT", str(tmp_path / "engine"))
        if hasattr(_mm, "CONFIG_PATH"):
            monkeypatch.setattr(
                _mm, "CONFIG_PATH", str(tmp_path / "config" / ".migrate_config.json"))

    # 2. Leaky env → cleared.
    for _var in _SANDBOX_CLEAR_ENV:
        monkeypatch.delenv(_var, raising=False)
    yield


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
    # The migrator's backup step writes to get_m3_engine_root()/backups. It does
    # NOT honor M3_BACKUP_DIR (prompt_backup_dir reads only the engine root + the
    # saved config value), so the previous M3_BACKUP_DIR override here was a
    # no-op and the template build leaked its backup to the real dir. Pin the
    # ENGINE ROOT (the knob that IS honored) into tmp so the backup lands here.
    env["M3_ENGINE_ROOT"] = str(tmp_root / "engine")
    env["M3_MEMORY_ROOT"] = str(tmp_root)

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
