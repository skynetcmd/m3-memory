#!/usr/bin/env python3
"""
Local embedding server — OpenAI-compatible /v1/embeddings endpoint.

Uses sentence-transformers to load Qwen3-Embedding-0.6B and serves on
port 1234 so memory_core.py and the test suite can use it without
LM Studio or Ollama.

Usage:
    python bin/embed_server.py                     # default: port 1234
    python bin/embed_server.py --port 9900
"""

from __future__ import annotations

import argparse
import logging
import time
from typing import Union

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

logging.basicConfig(level=logging.INFO, format="%(name)s: [%(levelname)s] %(message)s")
logger = logging.getLogger("embed_server")

DEFAULT_MODEL_ID = "Qwen/Qwen3-Embedding-0.6B"

app = FastAPI(title="M3 Embedding Server")
model: SentenceTransformer | None = None
model_name: str = ""
model_dim: int = 0


class EmbeddingRequest(BaseModel):
    model: str = ""
    input: Union[str, list[str]] = Field(...)


class EmbeddingData(BaseModel):
    object: str = "embedding"
    index: int
    embedding: list[float]


class EmbeddingResponse(BaseModel):
    object: str = "list"
    data: list[EmbeddingData]
    model: str
    usage: dict


class ModelInfo(BaseModel):
    id: str
    object: str = "model"
    owned_by: str = "local"


class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelInfo]


@app.get("/v1/models")
def list_models() -> ModelListResponse:
    return ModelListResponse(data=[ModelInfo(id=model_name)])


@app.post("/v1/embeddings")
def create_embeddings(req: EmbeddingRequest) -> EmbeddingResponse:
    texts = [req.input] if isinstance(req.input, str) else req.input
    t0 = time.perf_counter()
    vectors = model.encode(texts, normalize_embeddings=True).tolist()
    elapsed = time.perf_counter() - t0
    logger.info(f"Embedded {len(texts)} text(s) in {elapsed:.3f}s")
    total_tokens = sum(len(t.split()) for t in texts) * 2  # rough estimate
    return EmbeddingResponse(
        data=[EmbeddingData(index=i, embedding=v) for i, v in enumerate(vectors)],
        model=model_name,
        usage={"prompt_tokens": total_tokens, "total_tokens": total_tokens},
    )


@app.get("/health")
def health():
    return {"status": "ok", "model": model_name, "dim": model_dim}


def main():
    global model, model_name, model_dim
    parser = argparse.ArgumentParser(description="M3 Embedding Server")
    parser.add_argument("--model", default=DEFAULT_MODEL_ID, help="HuggingFace model ID")
    parser.add_argument("--port", type=int, default=1234, help="Port to serve on")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    args = parser.parse_args()

    model_name = "qwen3-embedding"
    logger.info(f"Loading {args.model}...")
    model = SentenceTransformer(args.model)
    model_dim = model.get_embedding_dimension()
    logger.info(f"Serving {model_name} (dim={model_dim}) on {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
