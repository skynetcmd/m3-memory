#!/usr/bin/env python3
"""
AMD GPU Optimized Embedding Proxy — delegates to llama-server.exe.
Handles <|endoftext|> appending and L2 normalization for Qwen3 GGUF.
Runs on Port 9903 by default.
"""

from __future__ import annotations
import argparse
import logging
import time
import subprocess
import os
import signal
import sys
import numpy as np
import httpx
import uvicorn
from fastapi import FastAPI
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
LLAMA_PORT = int(os.environ.get("LLAMA_PORT", "9904"))

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
        logger.error(f"llama-server error: {response.text}")
        return {"error": "llama-server failed", "details": response.text}

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
    llama_process = subprocess.Popen(cmd, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
    
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
        except:
            pass
        time.sleep(1)
    
    logger.error("llama-server failed to start in 30s")
    return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9903)
    parser.add_argument(
        "--host",
        default=os.environ.get("EMBED_SERVER_GPU_HOST", "127.0.0.1"),
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
                os.kill(llama_process.pid, signal.CTRL_BREAK_EVENT)
    else:
        sys.exit(1)

if __name__ == "__main__":
    main()
