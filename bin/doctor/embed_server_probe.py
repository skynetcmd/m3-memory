"""Rust-side `m3-embed-server doctor` subprocess wrapper.

Invokes the Rust binary's own diagnostic subcommand (B1 in m3-core-rs).
Best-effort: silently skips when the binary isn't on PATH (an install
without `m3 embedder install` is a legitimate state). Bounded timeout
prevents this phase from hanging.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

logger = logging.getLogger("memory.doctor.embed_server_probe")

BINARY_NAME = "m3-embed-server.exe" if sys.platform == "win32" else "m3-embed-server"
TIMEOUT_SECS = 30


def run() -> int:
    """Invoke `m3-embed-server doctor` and inherit its exit code.

    Returns 0 when the binary isn't on PATH (not a Python-side failure).
    Returns 1 on subprocess timeout or any unhandled exception.
    """
    exe = shutil.which(BINARY_NAME)
    if not exe:
        logger.debug(f"{BINARY_NAME} not on PATH; skipping Rust-side doctor")
        return 0
    print()
    print("=== Rust-side service health (m3-embed-server doctor) ===")
    # Quiet the llama.cpp/GGML backend's own stderr chatter — model-load notices
    # ("vocab missing newline token, using special_pad_id instead") and Metal
    # teardown lines ("ggml_metal_free", "llama_context ...") are INFO-level and
    # harmless for an embedding model, but with inherited stderr they interleave
    # with this probe's stdout and scroll the readable summary off-screen.
    # GGML_LOG_LEVEL=4 (error-only) suppresses them while still surfacing real
    # errors. Respect an operator-set value so power users can opt back in.
    env = {**os.environ, "GGML_LOG_LEVEL": os.environ.get("GGML_LOG_LEVEL", "4")}
    try:
        r = subprocess.run(
            [exe, "doctor"], capture_output=False, text=True,
            timeout=TIMEOUT_SECS, env=env,
        )
        return r.returncode
    except subprocess.TimeoutExpired:
        print(f"  m3-embed-server doctor timed out after {TIMEOUT_SECS}s")
        return 1
    except Exception as e:
        print(f"  m3-embed-server doctor failed: {type(e).__name__}: {e}")
        return 1
