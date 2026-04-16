"""Runtime task-category classifier for LongMemEval-style retrieval.

Infers the LongMemEval question category (single-session-user, multi-
session, temporal-reasoning, ...) from the raw question text at query
time, so a production agent can drive the same category-gated retrieval
knobs M3 uses for the benchmark without reading ground-truth metadata
from the dataset.

The classifier is a weighted k-NN over a balanced pool of ~3000 labeled
exemplars drawn from LongMemEval's upstream question bank (the pool it
was sampled FROM — test qids filtered out). Embeddings live in
data/longmemeval/task_classifier_exemplars.npz as an (N, 1024) L2-
normalized float32 matrix alongside a labels vector.

At query time we embed the question through the same qwen3-embedding
endpoint retrieval uses, dot-product against the exemplar matrix,
take the top-k most similar, and run similarity-weighted voting to
pick the category. Confidence is returned as the winning weight
divided by the total weight — callers can threshold it to fall back
to default retrieval knobs when the classifier is unsure.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from memory_core import _embed_many

_BASE_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_INDEX = _BASE_DIR / "data" / "longmemeval" / "task_classifier_exemplars.npz"

_TOP_K = 15


@dataclass
class _Index:
    embeddings: np.ndarray  # (N, D) float32, L2-normalized
    labels: np.ndarray  # (N,) object array of category strings


_INDEX: Optional[_Index] = None


def _load_index(path: Path = _DEFAULT_INDEX) -> _Index:
    global _INDEX
    if _INDEX is not None:
        return _INDEX
    if not path.exists():
        raise FileNotFoundError(
            f"task classifier index not found at {path}. "
            "Run bin/build_task_classifier_index.py to build it."
        )
    z = np.load(path, allow_pickle=True)
    emb = z["embeddings"]
    labels = z["labels"]
    if emb.dtype != np.float32:
        emb = emb.astype(np.float32)
    _INDEX = _Index(embeddings=emb, labels=labels)
    return _INDEX


async def _embed_one(text: str) -> np.ndarray:
    """Embed a single question through M3's configured embedder and
    return an L2-normalized float32 row vector. Matches the space the
    exemplar matrix lives in.
    """
    results = await _embed_many([text])
    vec, _model = results[0]
    if vec is None:
        raise RuntimeError("embedding failed for query text")
    v = np.asarray(vec, dtype=np.float32)
    n = float(np.linalg.norm(v))
    if n == 0.0:
        return v
    return v / n


async def classify_task_async(
    question: str,
    top_k: int = _TOP_K,
    index_path: Path = _DEFAULT_INDEX,
) -> tuple[str, float]:
    """Infer the LongMemEval category for `question`.

    Returns (predicted_category, confidence in [0, 1]).
    Confidence is winning-weight / total-weight over the top-k
    similarity-weighted votes — ~1.0 when the top-k all agree,
    ~0.17 (1/6) in the pathological uniform case.
    """
    idx = _load_index(index_path)
    qvec = await _embed_one(question)
    # Cosine sim = dot product because both sides are L2-normalized.
    sims = idx.embeddings @ qvec  # (N,)
    # Top-k indices; argpartition is O(N), then sort just the slice.
    k = min(top_k, sims.shape[0])
    part = np.argpartition(-sims, k - 1)[:k]
    order = part[np.argsort(-sims[part])]
    top_sims = sims[order]
    top_labels = idx.labels[order]

    # Weighted vote. Clip negative sims to 0 so an anti-correlated
    # exemplar never gets negative weight and flips the winner.
    weights = np.clip(top_sims, 0.0, None)
    totals: dict[str, float] = {}
    for lab, w in zip(top_labels, weights):
        totals[str(lab)] = totals.get(str(lab), 0.0) + float(w)
    if not totals:
        return "multi-session", 0.0
    winner = max(totals.items(), key=lambda kv: kv[1])
    total = sum(totals.values()) or 1.0
    return winner[0], winner[1] / total


def classify_task(
    question: str,
    top_k: int = _TOP_K,
    index_path: Path = _DEFAULT_INDEX,
) -> tuple[str, float]:
    """Synchronous wrapper around classify_task_async for callers that
    don't want to deal with the event loop. Creates a fresh loop if
    none is running.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and loop.is_running():
        raise RuntimeError(
            "classify_task called from inside a running event loop; "
            "use classify_task_async instead."
        )
    return asyncio.run(classify_task_async(question, top_k=top_k, index_path=index_path))


async def classify_many_async(
    questions: list[str],
    top_k: int = _TOP_K,
    index_path: Path = _DEFAULT_INDEX,
) -> list[tuple[str, float]]:
    """Batch classify — embeds every question in one _embed_many call,
    then does a single matmul against the exemplar matrix. ~N x faster
    than looping classify_task_async for smoke tests or eval.
    """
    if not questions:
        return []
    idx = _load_index(index_path)
    results = await _embed_many(questions)
    qvecs = np.zeros((len(questions), idx.embeddings.shape[1]), dtype=np.float32)
    for i, (v, _model) in enumerate(results):
        if v is None:
            raise RuntimeError(f"embedding failed for query #{i}")
        row = np.asarray(v, dtype=np.float32)
        n = float(np.linalg.norm(row))
        qvecs[i] = row / n if n > 0 else row

    sims = qvecs @ idx.embeddings.T  # (Q, N)
    k = min(top_k, sims.shape[1])
    out: list[tuple[str, float]] = []
    for i in range(sims.shape[0]):
        row = sims[i]
        part = np.argpartition(-row, k - 1)[:k]
        order = part[np.argsort(-row[part])]
        top_sims = row[order]
        top_labels = idx.labels[order]
        weights = np.clip(top_sims, 0.0, None)
        totals: dict[str, float] = {}
        for lab, w in zip(top_labels, weights):
            totals[str(lab)] = totals.get(str(lab), 0.0) + float(w)
        if not totals:
            out.append(("multi-session", 0.0))
            continue
        winner = max(totals.items(), key=lambda kv: kv[1])
        total = sum(totals.values()) or 1.0
        out.append((winner[0], winner[1] / total))
    return out
