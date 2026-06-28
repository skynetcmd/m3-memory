"""Smoke test for wave 9.4: observable embed-backend routing.

Sets M3_EMBED_GGUF so the in-process llama.cpp path is preferred. Calls
`_embed()` once and prints the returned vector dim plus the served-call
stats dict. Expected outcomes (one and only one key after one call):

  {'cuda-inprocess': 1}      — wheel built with --features embedded-cuda
  {'cpu-inprocess': 1}       — wheel built with --features embedded (CPU)
  {'cpu-http-fallback': 1}   — in-process unavailable, m3-embed-server up
  {'http-primary': 1}        — both above failed, M3_EMBED_URL path served

The CPU fallback at 127.0.0.1:8082 may not be running on this box; if so
the path silently moves on. That's intentional behavior under test.
"""
import asyncio
import os
import sys

os.environ.setdefault(
    "M3_EMBED_GGUF",
    r"C:\Users\username\.lmstudio\models\deepsweet\bge-m3-GGUF-Q4_K_M\bge-m3-GGUF-Q4_K_M.gguf",
)
# Do NOT set M3_EMBED_FALLBACK_URL — default 127.0.0.1:8082 exercises the
# real env. If that port isn't listening, the fallback raises ConnectionRefused
# and the path falls through to M3_EMBED_URL (or returns None).

sys.path.insert(0, r"C:\Users\username\m3-memory\bin")

from memory_core import (  # noqa: E402
    _embed,
    get_embed_backend_stats,
    reset_embed_backend_stats,
)


async def _main() -> None:
    reset_embed_backend_stats()
    vec, model = await _embed("hello world")
    if vec is None:
        print("embed returned None (all paths failed)")
    else:
        print(f"embed dim: {len(vec)}  model_tag: {model}")
    print(f"backend stats: {get_embed_backend_stats()}")


if __name__ == "__main__":
    asyncio.run(_main())
