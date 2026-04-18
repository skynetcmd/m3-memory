"""Build the task-category classifier index for LongMemEval.

Reads the raw upstream question pool from the custom_history_data dump,
filters out any question_id that appears in the LongMemEval-S test set,
maps the pool's fine-grained labels onto LongMemEval's six canonical
categories, samples a balanced exemplar set per category, embeds each
question via M3's configured embedder (qwen3-embedding through llama-
server), and writes the embeddings + labels to a .npz file the runtime
classifier loads at inference time.

The mapping from pool labels to LongMemEval categories is reverse-
engineered from 0822_all_500_questions_final_v2.json, which contains
exactly the 500 questions that became LongMemEval-S with their original
pool labels intact. Every pool label in the final-500 file maps to
exactly one LongMemEval category, so the mapping is deterministic.

Run once, check in the resulting .npz, then the `task_classifier`
module at query time just loads and cosine-sims. No benchmark-time
dataset access, no test-qid leakage.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR / "bin"))

# Route embeddings to the same llama-server M3 normally uses, so the
# classifier and retrieval layer see the question in the same embedding
# space. Match bench_longmemeval.py's setup.
os.environ.setdefault("LLM_ENDPOINTS_CSV", "http://localhost:8081/v1")
os.environ.setdefault("EMBED_BULK_CHUNK", "1024")
os.environ.setdefault("EMBED_BULK_CONCURRENCY", "4")

import numpy as np  # noqa: E402

from memory_core import _embed_many  # noqa: E402


POOL_LABEL_TO_CATEGORY = {
    "assistant_previnfo": "single-session-assistant",
    "implicit_preference": "single-session-preference",
    "implicit_preference_v2": "single-session-preference",
    "knowledge_update": "knowledge-update",
    "multi_session_synthesis": "multi-session",
    "single_hop": "single-session-user",
    "temp_reasoning_explicit": "temporal-reasoning",
    "temp_reasoning_implicit": "temporal-reasoning",
    "two_hop": "multi-session",
}


EXEMPLARS_PER_CATEGORY = 500
SEED = 42


def load_pool(pool_path: Path) -> list[dict]:
    with open(pool_path, encoding="utf-8") as f:
        return json.load(f)


def load_test_qids(test_path: Path) -> set[str]:
    with open(test_path, encoding="utf-8") as f:
        return {q["question_id"] for q in json.load(f)}


def sample_exemplars(
    pool: list[dict],
    test_qids: set[str],
    per_cat: int,
    rng: random.Random,
) -> list[tuple[str, str, str]]:
    """Return [(question_id, question_text, longmemeval_category), ...]
    with at most `per_cat` exemplars per category, no test-set leakage.
    """
    bucketed: dict[str, list[tuple[str, str]]] = {
        cat: [] for cat in set(POOL_LABEL_TO_CATEGORY.values())
    }
    for q in pool:
        qid = q.get("question_id")
        if not qid or qid in test_qids:
            continue
        cat = POOL_LABEL_TO_CATEGORY.get(q.get("question_type", ""))
        if cat is None:
            continue
        qc = q.get("question_content")
        if not isinstance(qc, dict):
            continue
        text = qc.get("question")
        if not isinstance(text, str) or not text.strip():
            continue
        bucketed[cat].append((qid, text.strip()))

    chosen: list[tuple[str, str, str]] = []
    for cat, items in sorted(bucketed.items()):
        rng.shuffle(items)
        take = items[:per_cat]
        for qid, text in take:
            chosen.append((qid, text, cat))
        print(f"  {cat}: pool={len(items)}, sampled={len(take)}")
    rng.shuffle(chosen)
    return chosen


async def embed_all(texts: list[str]) -> np.ndarray:
    """Embed every text via M3's configured embedder and return an
    (N, dim) float32 matrix. Rows that fail to embed raise loudly -
    we want the exemplar set to be complete, not silently short.
    """
    print(f"embedding {len(texts)} exemplars...")
    results = await _embed_many(texts)
    vecs: list[list[float]] = []
    for i, (v, _model) in enumerate(results):
        if v is None:
            raise RuntimeError(f"embedding failed for exemplar #{i}: {texts[i][:120]}")
        vecs.append(v)
    arr = np.asarray(vecs, dtype=np.float32)
    # L2-normalize so cosine similarity reduces to a dot product at
    # query time. Zero-vectors (shouldn't happen, but guard) stay zero.
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    arr = arr / norms
    return arr


async def main() -> None:
    pool_path = BASE_DIR / ".scratch" / "custom_history_data" / "2_questions" / "data_2_questions.json"
    test_path = BASE_DIR / "data" / "longmemeval" / "longmemeval_s_cleaned.json"
    out_path = BASE_DIR / "data" / "longmemeval" / "task_classifier_exemplars.npz"

    print(f"pool : {pool_path}")
    print(f"test : {test_path}")
    print(f"out  : {out_path}")

    pool = load_pool(pool_path)
    test_qids = load_test_qids(test_path)
    print(f"pool size: {len(pool):,}   test qids: {len(test_qids)}")

    rng = random.Random(SEED)  # nosec B311 - deterministic sampling for reproducibility, not cryptographic
    chosen = sample_exemplars(pool, test_qids, EXEMPLARS_PER_CATEGORY, rng)
    print(f"total exemplars chosen: {len(chosen)}")

    # Final paranoia check: no test qid survived.
    leaked = [qid for qid, _t, _c in chosen if qid in test_qids]
    if leaked:
        raise RuntimeError(f"test qids leaked into exemplar set: {leaked[:5]}")

    texts = [t for _qid, t, _c in chosen]
    labels = np.asarray([c for _qid, _t, c in chosen], dtype=object)
    qids = np.asarray([qid for qid, _t, _c in chosen], dtype=object)

    embeddings = await embed_all(texts)
    print(f"embeddings shape: {embeddings.shape}  dtype: {embeddings.dtype}")

    np.savez_compressed(
        out_path,
        embeddings=embeddings,
        labels=labels,
        qids=qids,
    )
    print(f"wrote {out_path}  ({out_path.stat().st_size / 1024:.1f} KiB)")


if __name__ == "__main__":
    asyncio.run(main())
