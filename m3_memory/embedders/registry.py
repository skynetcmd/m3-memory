from __future__ import annotations
import os
import json
import logging
import asyncio
import numpy as np
from typing import Any

logger = logging.getLogger(__name__)

class BGEM3Embedder:
    def __init__(self, mode: str = "dense", device: str = "cpu"):
        self.mode = mode
        try:
            from FlagEmbedding import BGEM3FlagModel
            self.model = BGEM3FlagModel('BAAI/bge-m3', use_fp16=True, device=device)
        except ImportError:
            logger.error("FlagEmbedding not installed. pip install FlagEmbedding")
            self.model = None

    def embed(self, texts: list[str]) -> dict:
        if not self.model:
            return {"dense": np.zeros((len(texts), 1024))}
        
        if self.mode == "dense":
            result = self.model.encode(texts, return_dense=True)
            return {"dense": np.array(result["dense"])}
        else:  # hybrid = dense + sparse
            result = self.model.encode(texts, return_dense=True, return_sparse=True)
            return {
                "dense": np.array(result["dense"]),
                "sparse": result["sparse"]
            }

class LlamaServerEmbedder:
    """Wrapper for the existing llama-server /embeddings endpoint."""
    def __init__(self, model: str = "qwen3-embedding:0.6b-q8"):
        self.model_name = model

    async def _embed_async(self, texts: list[str]) -> dict:
        from memory_core import _embed_many
        results = await _embed_many(texts)
        # _embed_many returns list[tuple[list[float] | None, str]]
        dense = []
        for vec, _m in results:
            if vec:
                dense.append(vec)
            else:
                # fallback for failed embeddings
                dense.append([0.0] * 1024) # assuming 1024 dim
        return {"dense": np.array(dense)}

    def embed(self, texts: list[str]) -> dict:
        """Sync wrapper for the async _embed_many."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        if loop.is_running():
            # This is tricky if called from an async context.
            # In bench_longmemeval.py it's called in an async run()
            # so we should probably provide an async embed.
            import nest_asyncio
            nest_asyncio.apply()
            return loop.run_until_complete(self._embed_async(texts))
        else:
            return loop.run_until_complete(self._embed_async(texts))

def get_embedder(model_name: str, **kwargs) -> Any:
    if "bge-m3" in model_name.lower():
        mode = kwargs.get("mode", "dense")
        return BGEM3Embedder(mode=mode)
    else:
        return LlamaServerEmbedder(model=model_name)
