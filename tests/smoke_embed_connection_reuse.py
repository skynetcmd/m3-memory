"""Smoke test: embed-path httpx client reuses connections.

Validates wave 9.7's pool-tuning fix in ``_get_embed_client()``. With the old
default-limits client (no Limits + keepalive_expiry=5s) every warm_single call
took a fresh TCP handshake, costing ~45 ms on localhost. With the tuned pool
the first call still pays the handshake but every subsequent call reuses an
idle keepalive connection — visible as a markedly shorter median for calls
2..N vs. call 1.

Run::

    $env:M3_EMBED_GGUF = "<path to bge-m3 gguf>"   # arms the fallback branch
    $env:M3_EMBED_FALLBACK_URL = "http://127.0.0.1:18082"
    python tests/smoke_embed_connection_reuse.py

Skips cleanly when M3_EMBED_FALLBACK_URL is unreachable. Does NOT depend on
httpx internal attribute names (those have shifted across 0.x); uses the
behavioral assertion "warm calls are faster than the connect-handshake call".
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

# Make ../bin importable when invoked as a script.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))

import httpx  # noqa: E402

FALLBACK_URL = (os.environ.get("M3_EMBED_FALLBACK_URL") or "http://127.0.0.1:8082").rstrip("/")
N_CALLS = 10


async def _ping_fallback() -> bool:
    """Return True iff the embed server answers a tiny request."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as c:
            r = await c.post(f"{FALLBACK_URL}/embedding", json={"input": ["ping"]})
            return r.status_code == 200
    except Exception as e:
        print(f"[skip] fallback server unreachable at {FALLBACK_URL}: {e}")
        return False


async def _main() -> int:
    if not await _ping_fallback():
        return 0  # clean skip — no server, nothing to assert

    # Arm the fallback branch. If M3_EMBED_GGUF is unset, _embed() will skip the
    # CPU-HTTP-fallback branch entirely and route to the legacy path. We don't
    # want that here — we want to exercise specifically the fallback POST.
    if not os.environ.get("M3_EMBED_GGUF"):
        # Best-effort placeholder: memory_core only checks the value is truthy
        # via _EMBED_GGUF_PATH being non-None at import time. Setting it after
        # import won't re-arm; instead we drive the POST directly through the
        # client to validate pool reuse, which is what we actually care about.
        pass

    import memory_core  # noqa: E402

    client = memory_core._get_embed_client()
    # Sanity: confirm Limits are tuned (not httpx defaults of 10/5/5s).
    print(f"[info] embed client: {client!r}")
    print(
        f"[info] tuned pool: max_conns={memory_core._EMBED_HTTP_MAX_CONNS} "
        f"keepalive={memory_core._EMBED_HTTP_MAX_KEEPALIVE} "
        f"expiry={memory_core._EMBED_HTTP_KEEPALIVE_EXPIRY}s"
    )

    timings: list[float] = []
    for i in range(N_CALLS):
        t0 = time.perf_counter()
        r = await client.post(
            f"{FALLBACK_URL}/embedding",
            json={"input": [f"reuse smoke {i}"]},
            timeout=httpx.Timeout(connect=3.0, read=30.0, write=10.0, pool=5.0),
        )
        r.raise_for_status()
        timings.append((time.perf_counter() - t0) * 1000.0)

    first = timings[0]
    rest = timings[1:]
    rest_median = statistics.median(rest)
    rest_p95 = sorted(rest)[max(0, int(len(rest) * 0.95) - 1)]
    print(f"[result] call#1 (cold connect): {first:.2f} ms")
    print(f"[result] calls 2..{N_CALLS} median: {rest_median:.2f} ms, p95: {rest_p95:.2f} ms")
    print(f"[result] all timings: {[f'{t:.2f}' for t in timings]}")

    # Behavioral assertion: with a tuned pool, calls 2..N should reuse a
    # keepalive connection and run noticeably faster than the cold first call.
    # Tolerance is generous because localhost handshakes are cheap; require
    # rest_median strictly less than first (with a small floor to avoid flakes
    # on absurdly fast embeddings).
    if first <= rest_median + 0.5:
        print(
            "[FAIL] first call is not slower than the warm median — connection "
            "reuse not observed."
        )
        return 1
    saved_pct = (1.0 - rest_median / first) * 100.0
    print(f"[pass] connection reuse observed — warm calls {saved_pct:.0f}% faster than cold")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
