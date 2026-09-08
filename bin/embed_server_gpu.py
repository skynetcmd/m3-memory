#!/usr/bin/env python3
"""
AMD GPU Optimized Embedding Proxy — delegates to llama-server.exe.
Handles <|endoftext|> appending and L2 normalization for Qwen3 GGUF.
Runs on Port 9903 by default.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import subprocess
import sys
import time

import httpx
import numpy as np
import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from m3_sdk import getenv_compat
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(name)s: [%(levelname)s] %(message)s")
logger = logging.getLogger("embed_proxy_gpu")

LLAMA_SERVER_EXE = os.environ.get(
    "LLAMA_SERVER_EXE",
    os.path.join(os.path.expanduser("~"), "llama.cpp", "bin", "llama-server.exe"),
)
GGUF_MODEL_PATH = os.environ.get(
    "GGUF_MODEL_PATH", os.path.join(os.getcwd(), "models", "qwen3-q8.gguf")
)
LLAMA_PORT = int(getenv_compat("M3_LLAMA_PORT", "LLAMA_PORT", "9904"))

app = FastAPI(title="M3 Qwen3 GPU Proxy")
llama_process = None

class EmbeddingRequest(BaseModel):
    model: str = ""
    input: str | list[str] = Field(...)

def l2_normalize(v):
    norm = np.linalg.norm(v)
    if norm < 1e-9: return v
    return (v / norm).tolist()

@app.post("/v1/embeddings")
async def create_embeddings(req: EmbeddingRequest):
    texts = [req.input] if isinstance(req.input, str) else req.input

    # 1. Append <|endoftext|> as required by Qwen3 GGUF logic
    processed_texts = [t + "<|endoftext|>" if not t.endswith("<|endoftext|>") else t for t in texts]

    t0 = time.perf_counter()

    async with httpx.AsyncClient() as client:
        # 2. Forward to llama-server
        # llama-server uses /embedding endpoint (standard) or /v1/embeddings
        # We'll use the llama-specific /embedding for more control if needed
        response = await client.post(
            f"http://localhost:{LLAMA_PORT}/v1/embeddings",
            json={"input": processed_texts, "model": "qwen3"},
            timeout=60.0
        )

    if response.status_code != 200:
        # Two bugs were here. (1) This returned HTTP 200 with an error BODY, so
        # a client saw success and then failed on a missing "data" key -- a
        # failure that looks like a client parsing bug rather than an upstream
        # error. (2) An n_ctx overflow was flattened into a generic failure,
        # discarding the token counts the client's subdivide-and-mean-pool
        # recovery needs (the same information loss as issue #139 on the
        # in-process server).
        #
        # Now: propagate the upstream status, and translate a context overflow
        # into the same structured 413 the in-process server emits so ONE client
        # contract covers every backend. The upstream body is preserved verbatim
        # so an older client's regex still matches it.
        body = response.text
        logger.error(f"llama-server error {response.status_code}: {body[:2000]}")
        low = body.lower()
        if re.search(r"(\d+)\s*tokens\s*>\s*n_ctx", body, re.I) or any(
            h in low for h in (
                "input too long", "context length exceeded",
                "maximum context length", "token length exceeds",
                "too many tokens", "exceeds context window",
            )
        ):
            m = re.search(r"(\d+)\s*tokens\s*>\s*n_ctx\s*(\d+)", body, re.I)
            err: "dict[str, object]" = {
                "code": "context_length_exceeded",
                "message": body[:2000],
            }
            if m:
                err["observed_tokens"] = int(m.group(1))
                err["max_tokens"] = int(m.group(2))
            logger.warning(
                "context_length_exceeded from llama-server. "
                "action: returning 413 so the client can subdivide and mean-pool. "
                "review: the CLIENT's chunk budget (M3_EMBED_TOKEN_BUDGET)."
            )
            return JSONResponse(status_code=413, content={"error": err})
        return JSONResponse(
            status_code=response.status_code,
            content={"error": "llama-server failed", "details": body[:2000]},
        )

    data = response.json()

    # 3. Apply L2 Normalization (llama-server currently lacks --embd-normalize for some builds)
    for entry in data["data"]:
        entry["embedding"] = l2_normalize(np.array(entry["embedding"]))

    elapsed = time.perf_counter() - t0
    logger.info(f"Embedded {len(texts)} text(s) via llama-server in {elapsed:.3f}s (Vulkan)")

    return data

def start_llama_server():
    global llama_process
    # Force -ngl 0 to use CPU only, leaving RTX 5080 for LM Studio.
    # Ryzen 9800X3D (Zen 5) is exceptionally fast for CPU embeddings.
    cmd = [
        LLAMA_SERVER_EXE,
        "-m", GGUF_MODEL_PATH,
        "--port", str(LLAMA_PORT),
        "-ngl", "0",
        "--embedding",
        "--pooling", "last",
        "--log-disable"
    ]

    logger.info(f"Starting llama-server: {' '.join(cmd)}")
    # CREATE_NEW_PROCESS_GROUP is Windows-only; getattr keeps this importable and
    # runnable on macOS/Linux (LLAMA_SERVER_EXE is env-overridable to a POSIX
    # llama-server), where a bare attribute reference would AttributeError (§1).
    _popen_kwargs = {}
    _flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    if _flags:
        _popen_kwargs["creationflags"] = _flags
    llama_process = subprocess.Popen(cmd, **_popen_kwargs)

    # Wait for server to be ready
    max_retries = 30
    for i in range(max_retries):
        try:
            import httpx
            with httpx.Client() as client:
                r = client.get(f"http://localhost:{LLAMA_PORT}/health")
                if r.status_code == 200:
                    logger.info("llama-server is ready.")
                    return True
        except Exception:
            pass
        time.sleep(1)

    logger.error("llama-server failed to start in 30s")
    return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9903)
    parser.add_argument(
        "--host",
        default=getenv_compat("M3_EMBED_SERVER_GPU_HOST", "EMBED_SERVER_GPU_HOST", "127.0.0.1"),
        help="Host to bind to (default 127.0.0.1; set 0.0.0.0 to serve on LAN)",
    )
    args = parser.parse_args()

    if not os.path.exists(GGUF_MODEL_PATH):
        logger.error(f"Model not found at {GGUF_MODEL_PATH}")
        sys.exit(1)

    if start_llama_server():
        try:
            uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        finally:
            if llama_process:
                logger.info("Stopping llama-server...")
                # CTRL_BREAK_EVENT is Windows-only and pairs with the process
                # group above; on POSIX use terminate() (SIGTERM). A bare
                # signal.CTRL_BREAK_EVENT reference AttributeErrors off Windows (§1).
                _brk = getattr(signal, "CTRL_BREAK_EVENT", None)
                if os.name == "nt" and _brk is not None:
                    os.kill(llama_process.pid, _brk)
                else:
                    llama_process.terminate()
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
