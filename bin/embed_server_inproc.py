#!/usr/bin/env python3
"""
Shared in-process GPU embedder server — one CUDA context, many thin clients.

WHY: `m3_core_rs.EmbeddedEmbedder` runs the GGUF embedder in-process. Every
process that embeds in-process opens its OWN CUDA context (multi-GB host
reservation) — CUDA contexts do not cross process boundaries, so the MCP memory
server and the cognitive loop each loaded a full copy (~4 GB + ~12 GB ≈ 18 GB on
SkyPC) for one embedder's worth of work. This server loads the embedder ONCE and
serves it over localhost HTTP; clients disable their own tier-1 (M3_EMBED_GGUF
unset + M3_EMBED_GGUF_AUTODETECT=0) and defer to it via M3_EMBED_FALLBACK_URL.

The win is HOST RAM, not latency: one CUDA context instead of one-per-process.
Measured on SkyPC (RTX 5080): a SINGLE small embed is P50 ~33 ms / P95 ~48 ms
via this server vs ~28 ms in-process — the localhost HTTP round-trip adds a few
ms (~10-15% on a single small request), not a rounding error. That per-call cost
AMORTISES across a batch (one round-trip for N vectors), so bulk paths — the
cognitive loop, file ingestion — see negligible overhead. Trade a few ms on
interactive single embeds for ~9-10 GB of host RAM back.

CONTRACT (matches the client tier-2 fallback in bin/memory/embed.py):
    POST /embedding        {"input": [texts...]}  -> {"data": [{"embedding": [...]}, ...]}  (input order)
    POST /v1/embeddings    {"model": ..., "input": str|list} -> OpenAI shape  (compat / tier-3)
    GET  /health           -> {"status", "model", "dim"}
Binary fast-path (high-volume ingestion): send `Accept: application/octet-stream`
to /embedding -> body = little-endian f32, row-major [n, dim], preceded by an
8-byte header (uint32 n, uint32 dim). numpy on both ends; no JSON float bloat.

HARDENING (§6): binds 127.0.0.1 only by default (the embedder is not a LAN
service); a semaphore serialises GPU calls; request size is capped. FAIL-LOUD
(§3): if the embedder can't load, exit non-zero with a clear message rather than
serve garbage. GPU-matrix (§1): the wheel picks CUDA/Metal/Vulkan/CPU; this
server is backend-agnostic — it just loads whatever EmbeddedEmbedder resolves.

Usage:
    python bin/embed_server_inproc.py                 # 127.0.0.1:8082 (the tier-2 default)
    python bin/embed_server_inproc.py --port 8082 --host 127.0.0.1
Model: M3_EMBED_GGUF env, else auto-detected (discover_bge_m3_gguf).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import struct
import sys
import time
from typing import Union

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# bin/ on path so sibling modules import when run as a script.
_BIN = os.path.dirname(os.path.abspath(__file__))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

logging.basicConfig(level=logging.INFO, format="%(name)s: [%(levelname)s] %(message)s")
logger = logging.getLogger("embed_server_inproc")

# Cap a single request's batch so a pathological caller can't OOM the GPU. Tune
# via M3_EMBED_SERVER_MAX_BATCH. The client's own EMBED_BULK_CHUNK is 1024, so
# this is a safety ceiling, not the expected batch size.
_MAX_BATCH = int(os.environ.get("M3_EMBED_SERVER_MAX_BATCH", "2048"))

# ── Two-lane admission gate (interactive fast-lane) ───────────────────────────
# WHY: the Rust `m3_core_rs.EmbeddedEmbedder` is NOT one serial CUDA context —
# it wraps `m3_dispatcher::Dispatcher<EmbeddedBackend>` with a POOL of
# `M3_EMBED_STREAMS` (default 8) independent contexts, its own slot semaphore,
# and a circuit breaker. `embed()` releases the GIL and its decode region is
# pure Rust, so it is SAFE to call concurrently — the dispatcher fans calls out
# across its stream pool without interleaving into a single context. (The old
# `Semaphore(1)` here predated the multi-stream dispatcher and throttled an
# 8-wide backend down to strictly serial — that serialisation, not any GPU
# limit, is what made an interactive query queue behind a bulk ingestion batch
# and wedge the single-event-loop MCP server.)
#
# POLICY: strict reservation (chosen 2026-07-14, user bhaba). Bulk is capped at
# `bulk_max = total - reserved` and NEVER exceeds it — even when the reserved
# slots are idle. `reserved` (default 1) slots are held for interactive
# single-query embeds so a search ALWAYS finds a free slot and never waits
# behind a bulk batch (batches are non-preemptible, so a work-conserving borrow
# would let bulk fill every slot and reintroduce the ~1-batch wait — the wedge
# this fix exists to kill). Interactive itself is NOT capped: when bulk is idle
# it may use up to `total`. The cost of strict reserve is that peak bulk
# concurrency is `total-1` instead of `total` during idle periods — accepted in
# exchange for a constant, load-independent interactive latency.
#
# `total` DEFAULTS TO THE BACKEND STREAM POOL (M3_EMBED_STREAMS, typically 8) —
# resolved from the loaded embedder in _resolve_admission(), so the server uses
# the full concurrency the Rust dispatcher offers rather than an arbitrary cap.
# Override with M3_EMBED_SERVER_CONCURRENCY (still clamped to the backend pool).
#
# Classification is by batch size (interactive posts 1 text, bulk posts up to
# EMBED_BULK_CHUNK=1024) with an explicit `X-M3-Embed-Priority` header override.
_INTERACTIVE_MAX_TEXTS = int(os.environ.get("M3_EMBED_INTERACTIVE_MAX_TEXTS", "8"))
# Slots reserved for interactive (never usable by bulk). >=1 guarantees a search
# always has a free slot.
_INTERACTIVE_RESERVED = int(os.environ.get("M3_EMBED_SERVER_INTERACTIVE_RESERVED", "1"))
# Total admission ceiling. 0 (the default sentinel) => use the backend stream
# pool as resolved in _resolve_admission(). A positive value overrides but is
# still clamped to the backend pool so we never over-subscribe the dispatcher.
_ADMISSION_TOTAL_DEFAULT = int(os.environ.get("M3_EMBED_SERVER_CONCURRENCY", "0"))


class _AdmissionGate:
    """Strict-reservation admission (no borrow).

    `total` concurrent GPU calls are allowed (clamped to the backend stream
    pool). At most `bulk_max = total - reserved` of them may be BULK requests,
    and bulk NEVER exceeds that — even when the reserved slots are idle. The
    `reserved` slots (>=1) are held for interactive single-query embeds so an
    interactive request ALWAYS finds a free slot and never waits behind a bulk
    batch. Interactive itself is not per-lane capped: when bulk is idle it may
    use up to `total`. In-flight batches are never preempted (bounded by
    _MAX_BATCH), but strict reservation means an interactive request never has
    to wait for one — there is always a slot bulk cannot occupy.
    """

    def __init__(self, total: int, reserved: int = 1):
        self.total = max(1, total)
        # Reserve at least 1 (guarantee an interactive slot) but leave bulk >=1.
        self.reserved = max(1, min(reserved, self.total - 1)) if self.total > 1 else 0
        self.bulk_max = self.total - self.reserved
        # Counts guarded by _cv.
        self._bulk_inflight = 0
        self._interactive_inflight = 0
        self._cv = asyncio.Condition()

    def _bulk_may_run_locked(self) -> bool:
        """Bulk admission (call under _cv): strictly capped at bulk_max. No
        borrow — the reserved slots are never available to bulk, so an
        interactive request always has room."""
        return self._bulk_inflight < self.bulk_max

    def _interactive_may_run_locked(self) -> bool:
        """Interactive admission (call under _cv): bounded only by the overall
        total. Because bulk can hold at most bulk_max = total - reserved, there
        are always >= reserved slots interactive can take immediately."""
        return (self._bulk_inflight + self._interactive_inflight) < self.total

    async def acquire(self, *, interactive: bool):
        async with self._cv:
            if interactive:
                while not self._interactive_may_run_locked():
                    await self._cv.wait()
                self._interactive_inflight += 1
            else:
                while not self._bulk_may_run_locked():
                    await self._cv.wait()
                self._bulk_inflight += 1
            self._cv.notify_all()

    async def release(self, *, interactive: bool):
        async with self._cv:
            if interactive:
                self._interactive_inflight -= 1
            else:
                self._bulk_inflight -= 1
            self._cv.notify_all()


# Resolved in _load_embedder() once the backend stream count is known; a plain
# Semaphore(1) fallback keeps import-time / test-time behaviour safe until then.
_GATE: _AdmissionGate | None = None
_GPU_SEM = asyncio.Semaphore(1)  # legacy fallback; superseded by _GATE at load


def _resolve_admission() -> _AdmissionGate:
    """Build the admission gate against the loaded backend's stream pool.

    `total` defaults to the backend's stream count (`_embedder.streams()` — the
    Rust dispatcher slot count == context-pool size, typically 8), so the server
    uses the full concurrency the dispatcher offers. A positive
    M3_EMBED_SERVER_CONCURRENCY (_ADMISSION_TOTAL_DEFAULT) overrides but is
    clamped to the pool so we never over-subscribe it. Strict reservation holds
    `_INTERACTIVE_RESERVED` (default 1) slot(s) for interactive so a search
    always has room; bulk_max = total - reserved.
    """
    streams = 0
    try:
        if _embedder is not None and hasattr(_embedder, "streams"):
            streams = int(_embedder.streams())
    except Exception:  # noqa: BLE001 — introspection is best-effort
        streams = 0
    # Backend pool (fall back to a sane 4 if the backend can't report it).
    pool = streams if streams > 0 else 4
    # total: full pool by default (_ADMISSION_TOTAL_DEFAULT==0 sentinel), else the
    # configured value, always clamped to the pool.
    total = pool if _ADMISSION_TOTAL_DEFAULT <= 0 else min(_ADMISSION_TOTAL_DEFAULT, pool)
    total = max(1, total)
    gate = _AdmissionGate(total=total, reserved=_INTERACTIVE_RESERVED)
    logger.info(
        "admission gate: total=%d (backend streams=%d, configured=%s), "
        "bulk_max=%d, interactive_reserved=%d (strict, no borrow)",
        gate.total, streams,
        _ADMISSION_TOTAL_DEFAULT if _ADMISSION_TOTAL_DEFAULT > 0 else "auto",
        gate.bulk_max, gate.reserved,
    )
    return gate


def _is_interactive(texts: list[str], request: "Request | None") -> bool:
    """Classify a request into the interactive fast-lane or the bulk lane.

    CONTRACT: the `X-M3-Embed-Priority: interactive|bulk` header is AUTHORITATIVE
    — it always wins. First-party clients set it explicitly (memory/embed.py
    tags single-query posts `interactive` and bulk posts `bulk`), so the size
    heuristic below is only a fallback for callers that send NO header.

    Fallback: batch size. A latency-sensitive caller sending a multi-text batch
    (> _INTERACTIVE_MAX_TEXTS) WITHOUT the header is treated as bulk and may
    queue behind other bulk work — such callers should send the header rather
    than rely on size. The default threshold (8) comfortably covers the only
    header-less interactive shape today (a single query embed, len 1)."""
    if request is not None:
        pri = (request.headers.get("x-m3-embed-priority") or "").strip().lower()
        if pri == "interactive":
            return True
        if pri == "bulk":
            return False
    return len(texts) <= _INTERACTIVE_MAX_TEXTS


app = FastAPI(title="M3 Shared In-Process GPU Embedder")

# Populated at startup by _load_embedder(); a failed load exits the process.
_embedder = None
_model_tag = ""
_dim = 0


class EmbeddingRequest(BaseModel):
    # `model` is optional (the tier-2 /embedding path omits it); accepted for the
    # OpenAI /v1/embeddings compat path.
    model: str = ""
    input: Union[str, list[str]] = Field(...)


def _already_serving(host: str, port: int, timeout: float = 1.5) -> bool:
    """True if a healthy embed server is already listening on host:port.

    Probes GET /health and accepts only a JSON body with status in
    {"ok","loading"} — i.e. it is THIS server, already up (or mid-load), not
    some unrelated service that happens to hold the port. Any connection error,
    timeout, non-200, or unrecognised body returns False so the caller starts
    normally. Stdlib-only (urllib) to keep the pre-flight dependency-free.

    Used as a pre-flight guard so a re-fired self-heal task never loads a second
    GPU embedder when one is already serving (§8: one CUDA context, not two).
    """
    import json as _json
    import urllib.request

    # 127.0.0.1 for a wildcard/loopback bind host so the probe targets a real IP.
    # nosec B104 — "0.0.0.0" here is a comparison literal used to REWRITE a wildcard
    # bind to loopback for the health probe; it is not a bind address. The actual
    # server bind defaults to 127.0.0.1 (see arg parser below, §6 hardening).
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host  # nosec B104
    url = f"http://{probe_host}:{port}/health"
    try:
        # nosec B310 — url is a fixed http:// loopback (probe_host is 127.0.0.1 or
        # the operator-set bind host); no user/file/custom scheme reaches urlopen.
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — fixed loopback URL  # nosec B310
            if resp.status != 200:
                return False
            body = _json.loads(resp.read().decode("utf-8", "replace"))
        return isinstance(body, dict) and body.get("status") in ("ok", "loading")
    except Exception:
        # Nothing there, refused, timed out, or a foreign service — start normally.
        return False


def _load_embedder() -> None:
    """Load the in-process GPU embedder ONCE. Fail loud (§3): a load failure
    exits non-zero — a shared embedder that can't embed must not pretend to."""
    global _embedder, _model_tag, _dim
    try:
        from memory.embed import _EMBED_GGUF_MODEL_TAG, discover_bge_m3_gguf

        from memory import config

        if config.m3_core_rs is None or not hasattr(config.m3_core_rs, "EmbeddedEmbedder"):
            logger.error(
                "m3_core_rs.EmbeddedEmbedder unavailable (wheel missing or built "
                "without --features embedded). Install with `m3 embedder install-gpu`. "
                "This server has nothing to serve — exiting."
            )
            sys.exit(2)

        gguf = (os.environ.get("M3_EMBED_GGUF") or "").strip() or discover_bge_m3_gguf()
        if not gguf:
            logger.error(
                "No bge-m3 GGUF found (set M3_EMBED_GGUF or place one in the model "
                "dirs). Nothing to serve — exiting."
            )
            sys.exit(2)

        t0 = time.perf_counter()
        emb = config.m3_core_rs.EmbeddedEmbedder(gguf)
        dim = emb.embedding_dim()
        if dim != config.EMBED_DIM:
            logger.error(
                "GGUF dim %d != EMBED_DIM %d — vector-space mismatch would poison "
                "the store. Exiting.", dim, config.EMBED_DIM
            )
            sys.exit(3)
        _embedder = emb
        _dim = dim
        _model_tag = _EMBED_GGUF_MODEL_TAG or config.EMBED_MODEL
        logger.info(
            "shared GPU embedder loaded (%s, dim=%d) in %.1fs — serving.",
            gguf, dim, time.perf_counter() - t0,
        )
        # Build the admission gate now that the backend stream pool is known.
        global _GATE
        _GATE = _resolve_admission()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — any load failure is fatal for a shared embedder
        logger.error("Embedder load failed (fatal): %s", e)
        sys.exit(1)


def _coerce_texts(inp: Union[str, list[str]]) -> list[str]:
    texts = [inp] if isinstance(inp, str) else list(inp)
    if not texts:
        return []
    if len(texts) > _MAX_BATCH:
        # Fail loud, don't silently truncate (§3): a truncated batch would drop
        # vectors and the caller would never know.
        raise ValueError(f"batch of {len(texts)} exceeds max {_MAX_BATCH} (M3_EMBED_SERVER_MAX_BATCH)")
    return texts


async def _embed(texts: list[str], *, interactive: bool = False) -> list[list[float]]:
    """Admission-gated GPU embed. Runs the blocking call off the event loop.

    Concurrency is bounded by the two-lane admission gate: interactive requests
    get reserved capacity, and bulk is strictly capped at total-reserved with NO
    borrow (the reserved slots are never available to bulk, so an interactive
    request always has room — see _AdmissionGate). The underlying
    `_embedder.embed` is safe to call concurrently — the Rust dispatcher fans the
    calls across its stream pool.
    """
    if _embedder is None:
        # Request arrived before _load_embedder() finished (racing startup).
        # Fail cleanly rather than AttributeError-ing inside the worker thread.
        raise RuntimeError("embedder not loaded yet")
    gate = _GATE
    if gate is None:
        # Pre-load / test fallback: preserve the original strict-serial behaviour.
        async with _GPU_SEM:
            return await asyncio.to_thread(_embedder.embed, texts)
    await gate.acquire(interactive=interactive)
    try:
        return await asyncio.to_thread(_embedder.embed, texts)  # type: ignore[union-attr]
    finally:
        await gate.release(interactive=interactive)


@app.post("/embedding")
async def embedding(req: EmbeddingRequest, request: Request):
    """Tier-2 contract: {"input":[...]} -> {"data":[{"embedding":[...]}]} in order.
    Binary fast-path when Accept: application/octet-stream."""
    try:
        texts = _coerce_texts(req.input)
    except ValueError as e:
        return JSONResponse(status_code=413, content={"error": str(e)})
    if not texts:
        return {"data": []}
    vecs = await _embed(texts, interactive=_is_interactive(texts, request))

    if "application/octet-stream" in (request.headers.get("accept") or ""):
        import numpy as np
        arr = np.asarray(vecs, dtype="<f4")
        header = struct.pack("<II", arr.shape[0], arr.shape[1] if arr.ndim == 2 else _dim)
        return Response(content=header + arr.tobytes(), media_type="application/octet-stream")

    # `index` is REQUIRED: the client's _order_embeddings (memory/chunking.py)
    # rejects any response whose items lack a complete index permutation — a
    # server that omits index (every item defaults to 0) would let mis-ordered
    # vectors pass a naive len-check and poison the store. We already return in
    # input order, but must SAY so via index.
    return {"data": [{"index": i, "embedding": v} for i, v in enumerate(vecs)]}


@app.post("/v1/embeddings")
async def v1_embeddings(req: EmbeddingRequest, request: Request):
    """OpenAI-compatible shape (tier-3 / LM-Studio-style callers)."""
    try:
        texts = _coerce_texts(req.input)
    except ValueError as e:
        return JSONResponse(status_code=413, content={"error": str(e)})
    if not texts:
        return {"object": "list", "data": [], "model": _model_tag, "usage": {}}
    vecs = await _embed(texts, interactive=_is_interactive(texts, request))
    return {
        "object": "list",
        "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vecs)],
        "model": _model_tag,
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


@app.get("/health")
def health():
    ok = _embedder is not None
    return {"status": "ok" if ok else "loading", "model": _model_tag, "dim": _dim}


@app.get("/v1/models")
def list_models():
    return {"object": "list", "data": [{"id": _model_tag, "object": "model"}]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Shared in-process GPU embedder server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("M3_EMBED_SERVER_PORT", "8082")))
    parser.add_argument(
        "--host", default=os.environ.get("M3_EMBED_SERVER_HOST", "127.0.0.1"),
        help="Bind host. Default 127.0.0.1 (loopback only — the embedder is not a "
             "LAN service). Set 0.0.0.0 ONLY deliberately.",
    )
    parser.add_argument("--log-file", default=None)
    args = parser.parse_args()

    if args.log_file:
        logging.getLogger().addHandler(logging.FileHandler(args.log_file, encoding="utf-8"))

    # Pre-flight liveness guard (do NOT stack a second embedder). The self-heal
    # scheduled task re-fires every minute; MultipleInstancesPolicy=IgnoreNew
    # covers a re-fire of the SAME task, but a manually-started server (or any
    # process not owned by the task) would otherwise let this instance load a
    # SECOND GPU embedder — a second multi-GB CUDA context, the exact waste this
    # shared-server design exists to avoid — and only THEN fail to bind the port.
    # So probe /health first: if the port is already serving, exit 0 cleanly
    # BEFORE touching the GPU. Any probe error (nothing there / half-up) falls
    # through and we start normally.
    if _already_serving(args.host, args.port):
        logger.info(
            "an embed server is already serving %s:%d — exiting without loading "
            "a second GPU embedder (§8: one CUDA context, not two).",
            args.host, args.port,
        )
        return 0

    _load_embedder()  # fail-loud: exits non-zero if the embedder can't load
    logger.info("listening on http://%s:%d (loopback=%s)",
                args.host, args.port, args.host in ("127.0.0.1", "localhost", "::1"))
    _run_server(args.host, args.port)
    return 0


def _run_server(host: str, port: int) -> None:
    """Run uvicorn in a way that survives a NO-CONSOLE launch (pythonw.exe).

    The scheduled task (AgentOS_EmbedServer) launches this via `pythonw.exe`,
    which has no console and no valid std handles. Under that launcher the
    plain `uvicorn.run(app, ...)` server loop terminated the moment it went
    live — uvicorn's default `run()` installs signal handlers and manages
    stdin/stdout for its shutdown/reload machinery, and with pythonw those
    handles are invalid, so `Server.serve()` returned immediately and the
    process exited right after logging "listening" (bind succeeded, then no
    one kept the loop alive).

    Fix: build the Server explicitly and drive it with asyncio.run(), with
    `install_signal_handlers=False` (the process is managed by the task /
    parent, not by Ctrl-C) so the loop lifecycle no longer depends on console
    or signal state that pythonw doesn't provide.
    """
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", use_colors=False,
    )

    class _NoSignalServer(uvicorn.Server):
        # Do NOT install SIGINT/SIGTERM handlers: under pythonw there is no
        # console to deliver them, and uvicorn's default attempt is part of why
        # the plain run() exits early on a no-console launch. The task/parent
        # stops us by terminating the process. (Overriding the method is
        # type-clean vs. reassigning the instance attribute.)
        def install_signal_handlers(self) -> None:
            return None

    server = _NoSignalServer(config)
    asyncio.run(server.serve())


def _ensure_std_streams() -> None:
    """Give the process real stdout/stderr when launched via pythonw.exe.

    Under `pythonw` (no console) sys.stdout / sys.stderr are None. A stray write
    from any dependency (llama.cpp C stdio, a logging fallback, a warning) then
    raises and can take the process down. Bind the missing streams to devnull so
    such writes are harmless. Idempotent; safe under normal python.exe (no-op).
    """
    import io
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))  # noqa: SIM115
            except Exception:  # noqa: BLE001 — best-effort; never block startup
                setattr(sys, name, io.StringIO())


if __name__ == "__main__":
    _ensure_std_streams()
    sys.exit(main())
