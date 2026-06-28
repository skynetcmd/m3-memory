"""Verify the m3-core-rs embedded (in-process llama.cpp) bge-m3 backend.

Standalone script (NOT a pytest test) — run directly:

    python tests/verify_embedded_bge_m3.py

Requires a wheel built with `--features embedded`:
    m3_core_rs.EmbeddedEmbedder must exist.

Checks:
  1. EmbeddedEmbedder constructs against the bge-m3 GGUF.
  2. embed() returns vectors of dimension EXACTLY 1024 (m3-memory EMBED_DIM).
  3. Sanity: non-zero, finite, uniform row length, semantic ordering.
  4. (optional) Cosine vs a vector already stored in agent_memory.db that
     was produced by the SAME GGUF file via the HTTP path.
"""

import math
import os
import sqlite3
import struct
import sys

GGUF = r"C:\Users\username\.lmstudio\models\deepsweet\bge-m3-GGUF-Q4_K_M\bge-m3-GGUF-Q4_K_M.gguf"
EMBED_DIM = 1024
DB = os.path.join(os.path.dirname(__file__), "..", "memory", "agent_memory.db")


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def fail(msg):
    print(f"\n!!! STOP: {msg}")
    sys.exit(1)


def main():
    print("=== verify_embedded_bge_m3 ===")

    try:
        import m3_core_rs
    except ImportError as e:
        fail(f"cannot import m3_core_rs: {e}")

    if not hasattr(m3_core_rs, "EmbeddedEmbedder"):
        fail("m3_core_rs has no EmbeddedEmbedder — wheel was NOT built with "
             "--features embedded")

    if not os.path.exists(GGUF):
        fail(f"GGUF model not found at {GGUF}")

    print(f"model: {GGUF}")
    emb = m3_core_rs.EmbeddedEmbedder(GGUF)
    print("EmbeddedEmbedder constructed (lazy — model not yet loaded)")

    # --- embedding_dim (forces lazy load) ----------------------------------
    dim_reported = emb.embedding_dim()
    print(f"embedding_dim() reported: {dim_reported}")

    # --- embed test strings ------------------------------------------------
    texts = ["hello world", "the quick brown fox", "machine learning",
             "hello there"]
    rows = emb.embed(texts)
    print(f"embed() returned {len(rows)} rows")

    if len(rows) != len(texts):
        fail(f"row count {len(rows)} != input count {len(texts)}")

    dims = {len(r) for r in rows}
    print(f"row dimensions: {dims}")
    if dims != {EMBED_DIM}:
        fail(f"dimension is {dims}, expected exactly {{{EMBED_DIM}}}. "
             "Model or quantization is wrong.")
    if dim_reported != EMBED_DIM:
        fail(f"embedding_dim() returned {dim_reported}, expected {EMBED_DIM}")

    print(f"OK: all rows are exactly {EMBED_DIM}-dim")

    # --- finite / non-zero -------------------------------------------------
    for i, r in enumerate(rows):
        if not all(math.isfinite(x) for x in r):
            fail(f"row {i} ({texts[i]!r}) has non-finite values (NaN/inf)")
        if not any(x != 0.0 for x in r):
            fail(f"row {i} ({texts[i]!r}) is all zeros")
    print("OK: all vectors finite and non-zero")

    print(f"sample row 0 ('hello world') first 6: "
          f"{[round(x, 5) for x in rows[0][:6]]}")

    # --- semantic sanity ---------------------------------------------------
    sim_close = cosine(rows[0], rows[3])   # hello world / hello there
    sim_far = cosine(rows[0], rows[2])     # hello world / machine learning
    print(f"cosine('hello world','hello there')   = {sim_close:.4f}")
    print(f"cosine('hello world','machine learning') = {sim_far:.4f}")
    if sim_close > sim_far:
        print("OK: semantically similar texts score higher than dissimilar")
    else:
        print("WARN: semantic ordering FAILED — similar texts did NOT score "
              "higher. Embeddings may be broken.")

    # --- optional: compare against a stored bge-m3 vector ------------------
    print("\n--- DB stored-vector parity check ---")
    db_path = os.path.abspath(DB)
    if not os.path.exists(db_path):
        print(f"SKIP: DB not found at {db_path}")
        return
    try:
        con = sqlite3.connect(db_path)
        cur = con.execute(
            "SELECT mi.content, me.embedding, me.dim "
            "FROM memory_embeddings me "
            "JOIN memory_items mi ON mi.id = me.memory_id "
            "WHERE me.embed_model = 'bge-m3-GGUF-Q4_K_M.gguf' "
            "AND me.dim = ? AND length(mi.content) BETWEEN 20 AND 400 "
            "LIMIT 3", (EMBED_DIM,))
        candidates = cur.fetchall()
        con.close()
    except Exception as e:
        print(f"SKIP: DB query failed: {e}")
        return

    if not candidates:
        print("SKIP: no stored bge-m3-GGUF-Q4_K_M.gguf vectors found")
        return

    all_high = True
    for content, blob, dim in candidates:
        stored = list(struct.unpack(f"<{dim}f", blob))
        re_embedded = emb.embed([content])[0]
        sim = cosine(stored, re_embedded)
        snippet = content[:50].replace("\n", " ")
        print(f"  cosine(stored, re-embedded) = {sim:.6f}  | {snippet!r}")
        if sim < 0.99:
            all_high = False

    if all_high:
        print("OK: re-embedded vectors match stored bge-m3 vectors "
              "(cosine >= 0.99) — same model confirmed")
    else:
        print("WARN: at least one cosine < 0.99 — embedded backend may differ "
              "from the HTTP path (pooling/normalization/quantization drift)")


if __name__ == "__main__":
    main()
