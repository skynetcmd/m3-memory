"""Capture behavioral baselines for the memory_core modularization.

Writes three artifacts under .scratch/migration_baseline/ that subsequent
phases use to detect drift. All read-only against agent_chatlog.db.
Random seeds fixed for determinism.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BIN_DIR = ROOT / "bin"
BASELINE_DIR = ROOT / ".scratch" / "migration_baseline"
sys.path.insert(0, str(BIN_DIR))

os.environ.setdefault("GGML_CUDA_DISABLE_GRAPHS", "1")
os.environ.setdefault(
    "M3_EMBED_GGUF",
    r"C:\Users\bhaba\.lmstudio\models\deepsweet\bge-m3-GGUF-Q4_K_M\bge-m3-GGUF-Q4_K_M.gguf",
)
os.environ.setdefault("M3_EMBED_STREAMS", "1")
os.environ.setdefault("M3_EMBED_CTX", "8192")
os.environ.setdefault("M3_EMBED_SEQ_MAX", "8")
os.environ.setdefault("M3_EMBED_N_BATCH", "8192")
os.environ.setdefault("M3_EMBED_N_UBATCH", "8192")

CHATLOG_DB = ROOT / "memory" / "agent_chatlog.db"
SEED = 42
N_EMBED_ROWS = 100
N_SEARCH_QUERIES = 50


def vec_sha256(vec: list[float]) -> str:
    """Stable hash with 6-decimal rounding (absorbs driver-level FP noise)."""
    import struct
    rounded = [round(x, 6) for x in vec]
    blob = struct.pack(f"{len(rounded)}f", *rounded)
    return hashlib.sha256(blob).hexdigest()


def sample_rows(n: int) -> list[tuple[str, str]]:
    uri = f"file:{CHATLOG_DB.as_posix()}?mode=ro&immutable=1"
    c = sqlite3.connect(uri, uri=True)
    cur = c.cursor()
    cur.execute(
        """SELECT id, content FROM memory_items
           WHERE is_deleted = 0 AND COALESCE(content,'') != ''
             AND LENGTH(content) BETWEEN 50 AND 20000
           ORDER BY id LIMIT 5000"""
    )
    cands = cur.fetchall()
    c.close()
    rng = random.Random(SEED)
    return rng.sample(cands, min(n, len(cands)))


def synth_queries(n: int) -> list[str]:
    uri = f"file:{CHATLOG_DB.as_posix()}?mode=ro&immutable=1"
    c = sqlite3.connect(uri, uri=True)
    cur = c.cursor()
    cur.execute(
        """SELECT content FROM memory_items
           WHERE is_deleted = 0 AND COALESCE(content,'') != ''
             AND LENGTH(content) BETWEEN 30 AND 200
           ORDER BY id LIMIT 1000"""
    )
    cands = [r[0] for r in cur.fetchall()]
    c.close()
    rng = random.Random(SEED + 1)
    return rng.sample(cands, min(n, len(cands)))


async def capture_embed_smoke():
    import memory_core as mc
    rows = sample_rows(N_EMBED_ROWS)
    print(f"  embedding {len(rows)} rows...")
    out = []
    t0 = time.perf_counter()
    for i, (mid, content) in enumerate(rows):
        vec, model = await mc._embed(content)
        if vec is None:
            out.append({"memory_id": mid, "vec_sha256": None, "model": model, "len": None})
        else:
            out.append({"memory_id": mid, "vec_sha256": vec_sha256(vec), "model": model, "len": len(vec)})
        if (i + 1) % 20 == 0:
            print(f"    {i+1}/{len(rows)}", flush=True)
    elapsed = time.perf_counter() - t0
    print(f"  done: {len(rows)} embeds in {elapsed:.1f}s ({len(rows)/elapsed:.1f}/s)")
    return {"seed": SEED, "n_rows": len(rows), "elapsed_s": round(elapsed, 2), "results": out}


async def capture_search_smoke():
    import memory_core as mc
    queries = synth_queries(N_SEARCH_QUERIES)
    print(f"  running {len(queries)} searches...")
    out = []
    t0 = time.perf_counter()
    for i, q in enumerate(queries):
        try:
            res = await mc.memory_search_impl(q, k=50)
            if isinstance(res, str):
                out.append({
                    "query": q[:80], "result_kind": "str", "result_len": len(res),
                    "result_sha256": hashlib.sha256(res.encode("utf-8")).hexdigest(),
                })
            else:
                out.append({
                    "query": q[:80], "result_kind": "obj",
                    "result_repr_sha256": hashlib.sha256(repr(res).encode("utf-8")).hexdigest(),
                })
        except Exception as e:
            out.append({"query": q[:80], "error": str(e)[:200]})
        if (i + 1) % 10 == 0:
            print(f"    {i+1}/{len(queries)}", flush=True)
    elapsed = time.perf_counter() - t0
    print(f"  done: {len(queries)} searches in {elapsed:.1f}s ({len(queries)/elapsed:.1f}/s)")
    return {"seed": SEED + 1, "n_queries": len(queries), "elapsed_s": round(elapsed, 2), "results": out}


async def capture_embedder_status():
    import memory_core as mc
    return await mc.embedder_status_impl()


async def main():
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Baseline directory: {BASELINE_DIR}")

    print("\n[1/3] embedder_status...")
    status = await capture_embedder_status()
    (BASELINE_DIR / "embedder_status.json").write_text(json.dumps(status, indent=2, sort_keys=True, default=str))
    print("  wrote embedder_status.json")

    print("\n[2/3] embed smoke (100 rows)...")
    embed_data = await capture_embed_smoke()
    (BASELINE_DIR / "embed_smoke.json").write_text(json.dumps(embed_data, indent=2, sort_keys=True))
    print("  wrote embed_smoke.json")

    print("\n[3/3] search smoke (50 queries)...")
    search_data = await capture_search_smoke()
    (BASELINE_DIR / "search_smoke.json").write_text(json.dumps(search_data, indent=2, sort_keys=True))
    print("  wrote search_smoke.json")

    print("\nBaseline capture complete.")


if __name__ == "__main__":
    asyncio.run(main())
