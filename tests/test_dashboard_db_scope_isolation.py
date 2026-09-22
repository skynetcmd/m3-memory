"""Dashboard store selection must be PER REQUEST, not process-global.

THE BUG THIS PINS. `selected_db` arrives as a cookie, so it is per-request, but
`set_active_db_env` wrote it to `os.environ["M3_DATABASE"]`, which is
process-global. Every dashboard handler is `async def` on one event loop, so two
overlapping requests raced:

    A (cookie main) sets main -> awaits -> B (cookie chatlog) sets chatlog
    -> A resumes and reads B's store.

`/api/stats` consequently labelled another viewer's store as "Main DB". The
value also outlived both requests, leaking into anything later in the process.

Compounding it, `resolve_db_path()` resolves `explicit > M3_DATABASE >
ContextVar`, so that env write also OVERRODE the `active_database(...)` blocks
already used elsewhere in the same file -- the correct pattern was being beaten
by the incorrect one.

`DbScopeMiddleware` binds the store through the `active_database` ContextVar,
which propagates across `await` inside a task and stays invisible to other
tasks. These tests pin the isolation, the operator override, and the two panels
that must NOT follow the selector.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import pytest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

pytest.importorskip("fastapi", reason="dashboard needs the [dashboard] extra")

from m3_core.paths import active_database, resolve_db_path  # noqa: E402

# ── The mechanism, independent of the dashboard ───────────────────────────────

def test_env_var_outranks_the_contextvar(monkeypatch, tmp_path):
    """Why the env write was not merely redundant but actively harmful.

    If this ever flips, `DbScopeMiddleware`'s deliberate yield-to-the-env-var
    branch is no longer needed -- and the comment explaining it is wrong.
    """
    main = str(tmp_path / "main.db")
    chat = str(tmp_path / "chat.db")

    monkeypatch.delenv("M3_DATABASE", raising=False)
    with active_database(chat):
        assert resolve_db_path(None) == os.path.abspath(chat)

    monkeypatch.setenv("M3_DATABASE", main)
    with active_database(chat):
        assert resolve_db_path(None) == os.path.abspath(main), (
            "M3_DATABASE must outrank the ContextVar; the middleware relies on "
            "this to let an operator's explicit pin win"
        )


def test_process_global_write_cross_contaminates_concurrent_tasks(monkeypatch, tmp_path):
    """The defect itself, reproduced: this is what the old code did.

    Kept as a live test rather than prose so the failure mode stays visible. It
    asserts that an ENV-VAR approach leaks -- if this ever stops leaking, the
    resolution order changed and this whole design should be revisited.
    """
    main = str(tmp_path / "main.db")
    chat = str(tmp_path / "chat.db")
    monkeypatch.delenv("M3_DATABASE", raising=False)
    seen: dict[str, str] = {}

    async def handler(name: str, path: str, delay: float) -> None:
        os.environ["M3_DATABASE"] = path      # what set_active_db_env used to do
        await asyncio.sleep(delay)
        seen[name] = resolve_db_path(None)

    async def drive() -> None:
        await asyncio.gather(
            handler("A", main, 0.05),
            handler("B", chat, 0.01),
        )

    try:
        asyncio.run(drive())
        assert seen["A"] == os.path.abspath(chat), (
            "expected the documented cross-contamination; if A saw its own "
            "store, the mechanism changed"
        )
    finally:
        os.environ.pop("M3_DATABASE", None)


def test_contextvar_isolates_concurrent_tasks(monkeypatch, tmp_path):
    """The fix: same interleaving, each task keeps its own store."""
    main = str(tmp_path / "main.db")
    chat = str(tmp_path / "chat.db")
    monkeypatch.delenv("M3_DATABASE", raising=False)
    seen: dict[str, str] = {}

    async def handler(name: str, path: str, delay: float) -> None:
        with active_database(path):
            await asyncio.sleep(delay)
            seen[name] = resolve_db_path(None)

    async def drive() -> None:
        await asyncio.gather(
            handler("A", main, 0.05),
            handler("B", chat, 0.01),
        )

    asyncio.run(drive())
    assert seen["A"] == os.path.abspath(main)
    assert seen["B"] == os.path.abspath(chat)


# ── The dashboard wiring ──────────────────────────────────────────────────────

def test_set_active_db_env_no_longer_writes_the_environment(monkeypatch):
    """The old function is retained as a no-op; it must stay a no-op.

    A future edit that "restores" the write reintroduces the race silently,
    because nothing else in the file would look wrong.
    """
    import dashboard_server as D

    monkeypatch.delenv("M3_DATABASE", raising=False)
    D.set_active_db_env("chatlog")
    assert "M3_DATABASE" not in os.environ, (
        "set_active_db_env must not write the process-global env var"
    )


def test_selected_db_store_maps_files_to_main(monkeypatch):
    """`files` is not a memory store, so it reads memory from main.

    Pinning the mapping because the refactor moved it into a new function; a
    silent change here would point the files view at a store that has no
    memory_items.
    """
    import dashboard_server as D

    monkeypatch.setattr(D, "_DB_PATHS", {
        "main": "/x/main.db", "chatlog": "/x/chat.db", "files": "/x/files.db",
    })
    assert D.selected_db_store("files") == "/x/main.db"
    assert D.selected_db_store("chatlog") == "/x/chat.db"
    assert D.selected_db_store("main") == "/x/main.db"
    assert D.selected_db_store("nonsense") == "/x/main.db", "unknown -> main"


def test_middleware_is_registered_inside_auth():
    """Auth must run OUTERMOST: no store is bound for a request about to 401.

    Starlette wraps each added middleware around the previous one, so the LAST
    registered executes FIRST. Auth is therefore registered last, and this test
    pins that relative order rather than an absolute index.
    """
    import dashboard_server as D

    classes = [m.cls for m in D.app.user_middleware]
    assert D.DbScopeMiddleware in classes
    assert D.DashboardAuthMiddleware in classes
    assert classes.index(D.DashboardAuthMiddleware) < classes.index(D.DbScopeMiddleware), (
        "auth must be registered after (i.e. run outside) the store binding"
    )
