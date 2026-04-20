from __future__ import annotations
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_RERANKER = None
_RERANKER_NAME = ""

def _get_reranker(model_name: str):
    """Lazy-load a sentence-transformers CrossEncoder, cached by name."""
    global _RERANKER, _RERANKER_NAME
    if _RERANKER is not None and _RERANKER_NAME == model_name:
        return _RERANKER
    
    try:
        from sentence_transformers import CrossEncoder
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        logger.info(f"Loading CrossEncoder {model_name} on {device}...")
        _RERANKER = CrossEncoder(model_name, device=device)
        _RERANKER_NAME = model_name
    except ImportError:
        logger.error("sentence_transformers not installed. pip install sentence-transformers")
        return None
    return _RERANKER

async def rerank_pool(query: str, candidates: list[dict], pool_k: int, model: str) -> list[dict]:
    """
    Shared primitive: rescore a list of candidates using a cross-encoder.
    
    Args:
        query: The user's question.
        candidates: List of item dicts from a bi-encoder search.
        pool_k: Max number of candidates to keep after reranking.
        model: Model name/path for the CrossEncoder.
    """
    if not candidates:
        return []
    
    ce = _get_reranker(model)
    if not ce:
        return candidates[:pool_k]
    
    t0 = time.perf_counter()
    # Cross-encoders expect list of (query, passage) pairs
    pairs = [(query, c.get("content", "")) for c in candidates]
    
    try:
        scores = ce.predict(pairs)
        for cand, score in zip(candidates, scores):
            cand["score"] = float(score)
            if "_explanation" in cand:
                cand["_explanation"]["rerank_score"] = float(score)
        
        candidates.sort(key=lambda x: x["score"], reverse=True)
        logger.debug(f"Reranked {len(pairs)} pairs in {time.perf_counter()-t0:.3f}s")
    except Exception as e:
        logger.error(f"Reranking failed: {e}")
        
    return candidates[:pool_k]
