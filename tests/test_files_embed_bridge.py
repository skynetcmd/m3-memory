"""files_memory.embed.embed_texts must not deadlock under a running loop.

The human CLI runs the SYNC files_ingest impl directly on the event-loop thread
(`asyncio.run(execute_tool_structured(...))` -> dispatch calls `spec.impl()`
inline). The old embed_texts, on detecting that running loop, scheduled the embed
coroutine back onto it via `run_coroutine_threadsafe(...).result()` — but the loop
was frozen executing the sync impl, so it could never step the coroutine:
`files_ingest` hung and committed nothing. The fix bridges through a private loop
on a worker thread when a loop is already running on THIS thread.

These run the whole call inside a worker thread and use a `result(timeout=...)` so
a regression DEADLOCKS the test (TimeoutError) instead of hanging the suite.
"""
import asyncio
import concurrent.futures
import sys
from pathlib import Path

_BIN = str(Path(__file__).resolve().parents[1] / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from files_memory import embed as E  # noqa: E402


def _patch(monkeypatch, model="fake"):
    async def _fake_embed_many(texts):
        return [([0.1, 0.2, 0.3], model) for _ in texts]
    # embed_texts does `from memory.embed import _embed_many` at call time, so
    # patch the attribute on whatever `sys.modules['memory.embed']` currently is.
    # The string target re-resolves the module at patch time — robust to other
    # tests that importlib.reload(memory.embed) and swap the module object.
    monkeypatch.setattr("memory.embed._embed_many", _fake_embed_many)


def test_empty_is_noop():
    assert E.embed_texts([]) == []


def test_no_running_loop_uses_asyncio_run(monkeypatch):
    _patch(monkeypatch)
    out = E.embed_texts(["hello"])
    assert [m for _, m in out] == ["fake"]
    assert out[0][0] == [0.1, 0.2, 0.3]


def test_running_loop_bridges_without_deadlock(monkeypatch):
    _patch(monkeypatch)

    async def caller():
        # Sync embed_texts invoked from INSIDE a running loop == the CLI scenario.
        return E.embed_texts(["a", "b"])

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        fut = ex.submit(lambda: asyncio.run(caller()))
        out = fut.result(timeout=15)  # old code deadlocks here -> TimeoutError

    assert [m for _, m in out] == ["fake", "fake"]
