"""The cognitive loop must honor SIGTERM on every platform.

asyncio's add_signal_handler is POSIX-only. On Windows it raises
NotImplementedError for BOTH SIGINT and SIGTERM, and the loop swallowed that
with a bare `pass` -- leaving NO graceful-shutdown path on the platform where m3
runs as a scheduled task. A SIGTERM/taskkill then killed it mid-write rather
than letting it checkpoint, close its DB handles, and deregister from the HALT
registry. Two consequences, both observed in the wild:

  - a torn WAL, the exact outcome the HALT protocol exists to prevent
  - an orphaned PID-registry entry, so the NEXT pre-flight sees a phantom writer
"""

import asyncio
import os
import signal
import sys

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)


def _install_like_the_loop(handler):
    """Mirror main_loop's handler-installation block."""
    async def _run():
        installed = {}
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, handler)
                installed[sig.name] = "asyncio"
            except (NotImplementedError, AttributeError, ValueError):
                try:
                    signal.signal(sig, lambda _s, _f: handler())
                    installed[sig.name] = "signal.signal"
                except (OSError, ValueError, AttributeError):
                    installed[sig.name] = None
        return installed
    return asyncio.run(_run())


def test_sigterm_handler_installs_on_this_platform():
    """The regression: on Windows this used to install NOTHING at all."""
    installed = _install_like_the_loop(lambda: None)
    assert installed["SIGTERM"] is not None, (
        "no SIGTERM handler installed -- the loop cannot shut down gracefully, "
        "so it dies mid-write and orphans its HALT registry entry"
    )
    assert installed["SIGINT"] is not None


def test_handler_only_sets_a_flag():
    """A Windows signal handler runs on the MAIN thread, not the event loop, so
    it must never touch loop state -- only flip the stop flag."""
    import inspect

    import m3_cognitive_loop as loop_mod

    src = inspect.getsource(loop_mod._signal_handler)
    assert "_STOP_EVENT.set()" in src
    for forbidden in ("await ", "loop.", "asyncio."):
        assert forbidden not in src, (
            f"_signal_handler touches {forbidden!r}; unsafe from a "
            "signal.signal handler on Windows"
        )


def test_signal_handler_takes_no_arguments():
    """It is installed both as an asyncio callback (no args) and wrapped in a
    lambda for signal.signal (2 args). The underlying function must take none."""
    import inspect

    import m3_cognitive_loop as loop_mod

    assert len(inspect.signature(loop_mod._signal_handler).parameters) == 0
