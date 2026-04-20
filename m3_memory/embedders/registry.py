from __future__ import annotations
import os
import json
import logging
import asyncio
import warnings
import numpy as np
from typing import Any

# Suppress repetitive tokenizer warnings from FlagEmbedding
warnings.filterwarnings("ignore", message=".*XLMRobertaTokenizerFast tokenizer.*")

logger = logging.getLogger(__name__)

# Global cache for embedder instances to avoid reloading models (Phase 2)
_EMBEDDER_CACHE: dict[str, Any] = {}

class BGEM3Embedder:
    def __init__(self, mode: str = "dense", device: str = "cpu"):
        self.mode = mode
        try:
            from FlagEmbedding import BGEM3FlagModel
            logger.info(f"Initializing BGEM3FlagModel on {device}...")
            self.model = BGEM3FlagModel('BAAI/bge-m3', use_fp16=False, device=device)
        except ImportError:
            logger.error("FlagEmbedding not installed. pip install FlagEmbedding")
            self.model = None

    def embed(self, texts: list[str]) -> dict:
        if not self.model:
            return {"dense": np.zeros((len(texts), 1024))}
        
        if self.mode == "dense":
            result = self.model.encode(texts, return_dense=True)
            return {"dense": np.array(result["dense_vecs"])}
        else:  # hybrid = dense + sparse
            result = self.model.encode(texts, return_dense=True, return_sparse=True)
            return {
                "dense": np.array(result["dense_vecs"]),
                "sparse": result["lexical_weights"]
            }

class LlamaServerEmbedder:
    """Wrapper for the existing llama-server /embeddings endpoint."""
    def __init__(self, model: str = "qwen3-embedding:0.6b-q8"):
        self.model_name = model

    async def _embed_async(self, texts: list[str]) -> dict:
        from memory_core import _embed_many
        results = await _embed_many(texts)
        dense = []
        for vec, _m in results:
            if vec:
                dense.append(vec)
            else:
                dense.append([0.0] * 1024)
        return {"dense": np.array(dense)}

    def embed(self, texts: list[str]) -> dict:
        """Sync wrapper for the async _embed_many."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        if loop.is_running():
            import nest_asyncio
            nest_asyncio.apply()
            return loop.run_until_complete(self._embed_async(texts))
        else:
            return loop.run_until_complete(self._embed_async(texts))

def get_embedder(model_name: str, **kwargs) -> Any:
    cache_key = f"{model_name}:{kwargs.get('mode', 'dense')}"
    if cache_key in _EMBEDDER_CACHE:
        return _EMBEDDER_CACHE[cache_key]
    
    if "bge-m3" in model_name.lower():
        mode = kwargs.get("mode", "dense")
        instance = BGEM3Embedder(mode=mode)
    else:
        instance = LlamaServerEmbedder(model=model_name)
    
    _EMBEDDER_CACHE[cache_key] = instance
    return instance
