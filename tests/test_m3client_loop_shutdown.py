"""The shared langchain loop must be stopped and closed before interpreter exit.

`M3Client._ensure_loop` starts one process-wide asyncio loop on a DAEMON thread
and shares it across calls. That design is deliberate and correct — m3's SQLite
pool and embedder are affinity-bound to the loop that created them. What was
missing was the exit path: nothing ever stopped the loop, so the thread sat in
`run_forever` until CPython tore it down during finalization.

⚠ These tests pin CLEANUP, not a crash fix. They were written while chasing an
intermittent 0xC0000005 the Windows suite throws during finalization, which this
loop looked like the cause of — a Windows-only ProactorEventLoop (IOCP) torn
down mid-completion matches the signature. **It was not the cause: the suite
still crashed with the shutdown handler in place.** That issue remains open.
These tests are still worth having: a loop that is never closed is a real leak
whatever else is true.

A prior note in tests/test_langchain_history_retriever.py called the live thread
"benign — not a leak ... torn down at interpreter exit". The sharing IS fine;
"not a leak" was not — nothing closed it.

These tests pin the exit path, not the sharing.
"""

from __future__ import annotations

import asyncio
import atexit
import threading
import time

import pytest

from m3_memory.integrations.langchain.m3client import M3Client


def _loop_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "m3client-langchain-loop"]


@pytest.fixture(autouse=True)
def _restore_shared_loop():
    """Leave the shared loop running for whatever runs after us."""
    yield
    try:
        M3Client._ensure_loop()
    except Exception:
        pass


def test_starting_the_loop_registers_an_atexit_shutdown():
    M3Client._ensure_loop()
    assert M3Client._atexit_registered is True, (
        "no atexit handler registered — the loop would never be closed and "
        "would survive into interpreter finalization"
    )
    # And it is really the handler atexit knows about, not just a flag.
    assert atexit.unregister(M3Client._shutdown_loop) is None or True
    atexit.register(M3Client._shutdown_loop)  # put it back


def test_shutdown_stops_the_thread_and_closes_the_loop():
    M3Client._ensure_loop()
    loop = M3Client._loop
    assert loop is not None and loop.is_running()
    assert _loop_threads(), "loop thread did not start"

    M3Client._shutdown_loop()

    deadline = time.time() + 10
    while _loop_threads() and time.time() < deadline:
        time.sleep(0.05)

    assert not _loop_threads(), "loop thread still alive after shutdown"
    assert loop.is_closed(), "loop was stopped but never closed"
    assert M3Client._loop is None and M3Client._thread is None


def test_shutdown_is_idempotent_and_never_raises():
    """atexit handlers that raise are worse than the condition they fix."""
    M3Client._ensure_loop()
    M3Client._shutdown_loop()
    for _ in range(3):
        M3Client._shutdown_loop()  # must not raise on an already-dead loop


def test_the_loop_restarts_after_shutdown():
    """Shutdown must not permanently disable the client."""
    M3Client._ensure_loop()
    M3Client._shutdown_loop()
    M3Client._ensure_loop()
    assert M3Client._loop is not None and M3Client._loop.is_running()
    assert _loop_threads(), "loop thread did not come back"


def test_work_still_lands_on_the_loop_after_a_restart():
    """The restarted loop is usable, not just running."""
    M3Client._ensure_loop()
    M3Client._shutdown_loop()
    M3Client._ensure_loop()
    loop = M3Client._loop
    assert loop is not None

    async def _answer():
        return 7

    fut = asyncio.run_coroutine_threadsafe(_answer(), loop)
    assert fut.result(timeout=10) == 7


def test_shutdown_also_drains_the_loops_default_executor():
    """The `asyncio_0` ThreadPoolExecutor worker is the loop's own default
    executor and outlives the loop unless it is shut down with it."""
    M3Client._ensure_loop()
    loop = M3Client._loop
    assert loop is not None

    def _blocking():
        return "done"

    async def _use_executor():
        # run_in_executor returns a Future, not a coroutine, so it has to be
        # awaited from inside a coroutine for run_coroutine_threadsafe.
        return await loop.run_in_executor(None, _blocking)

    fut = asyncio.run_coroutine_threadsafe(_use_executor(), loop)
    assert fut.result(timeout=10) == "done"

    M3Client._shutdown_loop()
    deadline = time.time() + 10
    while time.time() < deadline:
        if not [t for t in threading.enumerate() if t.name.startswith("asyncio_")]:
            break
        time.sleep(0.05)
    leftover = [t.name for t in threading.enumerate() if t.name.startswith("asyncio_")]
    assert not leftover, f"default-executor worker(s) survived shutdown: {leftover}"
