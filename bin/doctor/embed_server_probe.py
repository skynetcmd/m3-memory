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
_HEALTH_PORT = 8082  # the port shared_embedder_probe also checks
TIMEOUT_SECS = 30


def _resolve_binary() -> "str | None":
    """Locate the Rust embed-server binary, PATH or not.

    `shutil.which` alone is the #167 defect. The binary normally lives inside
    the m3_core_rs wheel (site-packages/m3_core_rs/m3-embed-server[.exe]) and is
    NOT on PATH, so a bare which() returns None while the service is installed
    and serving. Measured 2026-09-12 on two hosts: which() -> None,
    embedder_admin._server_binary() -> the real path, :8082/health -> 200.
    """
    exe = shutil.which(BINARY_NAME)
    if exe:
        return exe
    try:
        from m3_memory import embedder_admin
        found = embedder_admin._server_binary()
        # _server_binary returns a Path; subprocess and the callers here want a
        # str. Normalising at the boundary keeps the rest of this module typed.
        return str(found) if found else None
    except Exception:  # noqa: BLE001 — a probe must never raise
        return None


def _port_answers(timeout: float = 2.0) -> bool:
    """Is something serving on :8082 right now?

    The distinction that matters for the verdict: a binary we cannot find but a
    port that answers means the service IS running, however it was started.
    Reporting "not installed" there is the false alarm §3 names -- and it is
    exactly what happened, in the same doctor run whose embedding-cascade line
    said "healthy -- shared tier-2 embedder online".
    """
    import urllib.request
    try:
        with urllib.request.urlopen(  # nosec B310 — fixed loopback URL
            f"http://127.0.0.1:{_HEALTH_PORT}/health", timeout=timeout
        ) as r:
            return 200 <= r.status < 300
    except Exception:  # noqa: BLE001
        return False


def run(brief: bool = False) -> int:
    """Invoke `m3-embed-server doctor` and inherit its exit code.

    Returns 0 when the binary isn't installed (not a Python-side failure).
    Returns 1 on subprocess timeout or any unhandled exception. brief=True
    captures the subprocess output and prints only a one-line verdict.
    """
    exe = _resolve_binary()
    if not exe:
        # Three states, not two. "Cannot find the binary" and "the service is
        # absent" are different claims, and conflating them reported a live
        # server as missing (#167).
        if _port_answers():
            if brief:
                print("embed-server: serving on :8082 (binary not located)")
            else:
                logger.debug(
                    f"{BINARY_NAME} not located, but :{_HEALTH_PORT} answers — "
                    f"the service is running; skipping the Rust-side doctor"
                )
        else:
            if brief:
                print("embed-server: not installed (optional)")
            else:
                logger.debug(f"{BINARY_NAME} not located; skipping Rust-side doctor")
        return 0
    if not brief:
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
        # In brief mode, capture (and discard) the subprocess's own noisy output;
        # we only report pass/fail from its exit code as a single line.
        # `doctor` does not exist on every build of this binary. Measured
        # 2026-09-12 against m3_core_rs 3.9.7: `m3-embed-server doctor` prints
        # USAGE and exits 2, while `status` works and reports
        # running/stopped/not installed. Treating that rc 2 as a health FAILURE
        # reported a fine install as broken -- the same false alarm #167 is
        # about, one layer down. Probe the subcommand instead of assuming it.
        sub = "doctor"
        probe = subprocess.run(
            [exe, "doctor"], capture_output=True, text=True,
            timeout=TIMEOUT_SECS, env=env,
        )
        if probe.returncode != 0 and "USAGE" in (probe.stdout or "") + (probe.stderr or ""):
            sub = "status"
            r = subprocess.run(
                [exe, "status"], capture_output=brief, text=True,
                timeout=TIMEOUT_SECS, env=env,
            )
        else:
            r = probe
            if not brief:
                # The probe already consumed the output; re-emit it.
                print(r.stdout or "", end="")

        if brief:
            if r.returncode == 0:
                state = (r.stdout or "").strip().splitlines()[-1:] or [""]
                detail = f" ({state[0]})" if sub == "status" and state[0] else ""
                print(f"✅ embed-server: ok{detail}")
            else:
                print(f"❌ embed-server: FAILED ({sub})")
        return r.returncode
    except subprocess.TimeoutExpired:
        print("embed-server: ❌ timed out" if brief
              else f"  m3-embed-server doctor timed out after {TIMEOUT_SECS}s")
        return 1
    except Exception as e:
        print("embed-server: ❌ error" if brief
              else f"  m3-embed-server doctor failed: {type(e).__name__}: {e}")
        return 1
