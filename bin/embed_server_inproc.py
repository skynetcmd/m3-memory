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
# Serialise GPU calls: the embedder is one CUDA context; concurrent Python
# callers must not interleave into it. Semaphore(1) = strict serialisation
# (correct + simplest); raise only if the backend is proven concurrency-safe.
_GPU_SEM = asyncio.Semaphore(int(os.environ.get("M3_EMBED_SERVER_CONCURRENCY", "1")))

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
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{probe_host}:{port}/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — fixed loopback URL
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


async def _embed(texts: list[str]) -> list[list[float]]:
    """Serialised GPU embed. Runs the blocking call off the event loop."""
    async with _GPU_SEM:
        return await asyncio.to_thread(_embedder.embed, texts)  # type: ignore[union-attr]


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
    vecs = await _embed(texts)

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
async def v1_embeddings(req: EmbeddingRequest):
    """OpenAI-compatible shape (tier-3 / LM-Studio-style callers)."""
    try:
        texts = _coerce_texts(req.input)
    except ValueError as e:
        return JSONResponse(status_code=413, content={"error": str(e)})
    if not texts:
        return {"object": "list", "data": [], "model": _model_tag, "usage": {}}
    vecs = await _embed(texts)
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
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
