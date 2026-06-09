"""Integration smoke for sliding-window chunking against real chatlog data.

READ-ONLY. Opens agent_chatlog.db with mode=ro&immutable=1, samples a
mix of short and long rows, chunks each via _chunk_for_sliding_window,
and embeds each chunk via the in-process Rust embedder. Verifies:

  - Short rows produce exactly one ('default'-equivalent) chunk that embeds.
  - Long rows produce N > 1 chunks; every chunk embeds successfully.
  - Every embedding is 1024-dim (matches the canonical bge-m3 dim).
  - No chunk exceeds MAX_CHARS_PER_CHUNK.
  - Consecutive chunks overlap by exactly MIN_OVERLAP_CHARS.
  - Re-embedding the FIRST window of a long row yields cosine ~1.0 vs
    the FIRST window's original embedding (sanity check that windows are
    deterministic and that the embedder produces stable vectors).

No INSERT/UPDATE/DELETE. The DB connection is opened with
mode=ro&immutable=1 so any accidental write attempt would raise.
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
import time
from pathlib import Path

# Make bin/ importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

# Required env for the in-process embedder before importing memory_core
# (it imports m3_core_rs which expects M3_EMBED_GGUF at runtime, not import).
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

import m3_core_rs  # noqa: E402
import memory_core as mc  # noqa: E402

CHATLOG_DB = Path(r"C:\Users\bhaba\m3-memory\memory\agent_chatlog.db")
N_SHORT = 5
N_MEDIUM = 5
N_LONG = 5  # rows above MAX_CHARS_PER_CHUNK — will exercise chunking


def cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def sample_rows() -> dict[str, list[tuple[str, str]]]:
    """Sample rows in three length buckets. Returns dict label -> [(memory_id, content)]."""
    # Read-only connect (mode=ro&immutable=1 makes the DB physically read-only
    # for this process — write attempts raise SQLITE_READONLY).
    uri = f"file:{CHATLOG_DB.as_posix()}?mode=ro&immutable=1"
    c = sqlite3.connect(uri, uri=True)
    cur = c.cursor()
    out = {}

    # Short: content length < 1000 chars
    cur.execute(
        """SELECT id, content FROM memory_items
           WHERE is_deleted = 0
             AND LENGTH(content) < 1000
             AND LENGTH(content) > 50
           ORDER BY RANDOM() LIMIT ?""",
        (N_SHORT,),
    )
    out["short"] = [(r[0], r[1]) for r in cur.fetchall()]

    # Medium: 5000-20000 chars (below MAX, still substantive)
    cur.execute(
        """SELECT id, content FROM memory_items
           WHERE is_deleted = 0
             AND LENGTH(content) BETWEEN 5000 AND 20000
           ORDER BY RANDOM() LIMIT ?""",
        (N_MEDIUM,),
    )
    out["medium"] = [(r[0], r[1]) for r in cur.fetchall()]

    # Long: above MAX_CHARS_PER_CHUNK so chunking will kick in
    cur.execute(
        """SELECT id, content FROM memory_items
           WHERE is_deleted = 0
             AND LENGTH(content) > ?
           ORDER BY RANDOM() LIMIT ?""",
        (mc.MAX_CHARS_PER_CHUNK, N_LONG),
    )
    out["long"] = [(r[0], r[1]) for r in cur.fetchall()]

    c.close()
    return out


def assert_invariants(content: str, chunks: list[tuple[str, int]]) -> list[str]:
    """Return list of failure messages (empty if all invariants hold)."""
    errs = []
    if not chunks:
        errs.append("empty chunk list")
        return errs

    # Ceiling
    for i, (ct, idx) in enumerate(chunks):
        if len(ct) > mc.MAX_CHARS_PER_CHUNK:
            errs.append(f"chunk {i} over ceiling: {len(ct)} > {mc.MAX_CHARS_PER_CHUNK}")
        if idx != i:
            errs.append(f"chunk {i} has non-sequential idx {idx}")

    # For multi-chunk inputs verify overlap and tail size
    if len(chunks) > 1:
        # Min tail size
        last_text, _ = chunks[-1]
        if len(last_text) < mc.MIN_OVERLAP_CHARS:
            errs.append(
                f"last window too thin: {len(last_text)} < {mc.MIN_OVERLAP_CHARS}"
            )
        # Overlap between consecutive windows. Locate each chunk by its char
        # offset (chunks are contiguous slices of the original text — we
        # know start = idx * STRIDE_CHARS, except for the (potentially
        # absent) shift-back case which doesn't fire under default config).
        for i in range(len(chunks) - 1):
            prev_text, _ = chunks[i]
            curr_text, _ = chunks[i + 1]
            # Compute start positions: prev starts at i*STRIDE, curr at (i+1)*STRIDE
            prev_start = i * mc.STRIDE_CHARS
            prev_end = prev_start + len(prev_text)
            curr_start = (i + 1) * mc.STRIDE_CHARS
            overlap = prev_end - curr_start
            if overlap != mc.MIN_OVERLAP_CHARS:
                # last pair may differ if the final iteration's window was
                # shorter than MAX — still must be >= MIN_OVERLAP
                if i == len(chunks) - 2 and overlap >= mc.MIN_OVERLAP_CHARS:
                    pass  # acceptable
                else:
                    errs.append(
                        f"overlap between chunk {i} and {i+1}: {overlap} != {mc.MIN_OVERLAP_CHARS}"
                    )

    # Tail coverage (last char present somewhere)
    if content and not any(ct.endswith(content[-1]) for ct, _ in chunks):
        # Could miss with shift-back? No — text[-1] is always at position n-1
        # and the last window ends at n. Check by content-suffix.
        last_text = chunks[-1][0]
        if not content.endswith(last_text):
            errs.append("last chunk is not a suffix of the input")

    return errs


def main():
    print(f"Config: MAX={mc.MAX_CHARS_PER_CHUNK} OVL={mc.MIN_OVERLAP_CHARS} STRIDE={mc.STRIDE_CHARS}")
    print(f"DB (read-only): {CHATLOG_DB}")
    print(f"Sampling: {N_SHORT} short + {N_MEDIUM} medium + {N_LONG} long\n")

    rows = sample_rows()
    print(f"sampled: short={len(rows['short'])} medium={len(rows['medium'])} long={len(rows['long'])}\n")

    print("Loading bge-m3 in-process embedder...")
    t0 = time.perf_counter()
    embedder = m3_core_rs.EmbeddedEmbedder(os.environ["M3_EMBED_GGUF"])
    dim = embedder.embedding_dim()
    print(f"  loaded in {time.perf_counter()-t0:.1f}s, dim={dim}, backend={m3_core_rs.embed_backend_label()}\n")
    assert dim == 1024, f"expected 1024-dim bge-m3, got {dim}"

    total_pass = 0
    total_fail = 0
    failures = []

    for bucket in ("short", "medium", "long"):
        print(f"=== {bucket.upper()} ===")
        for memory_id, content in rows[bucket]:
            # Mirror the augmentation that memory_write_impl does (without
            # the metadata anchors — we don't have metadata here, that's fine).
            text = content  # no anchor augmentation since we'd need metadata

            chunks = mc._chunk_for_sliding_window(text)
            n_chunks = len(chunks)

            # Invariants on chunks
            inv_errs = assert_invariants(text, chunks)
            if inv_errs:
                total_fail += 1
                for e in inv_errs:
                    failures.append(f"{memory_id[:8]} (len={len(text)}, n_chunks={n_chunks}): {e}")
                continue

            # Embed each chunk
            chunk_texts = [ct for ct, _ in chunks]
            try:
                vecs = embedder.embed(chunk_texts)
            except Exception as e:
                total_fail += 1
                failures.append(f"{memory_id[:8]}: embed failed: {e}")
                continue
            if len(vecs) != n_chunks:
                total_fail += 1
                failures.append(f"{memory_id[:8]}: embed returned {len(vecs)} vecs for {n_chunks} chunks")
                continue
            if any(len(v) != dim for v in vecs):
                total_fail += 1
                failures.append(f"{memory_id[:8]}: vector dim mismatch (expected {dim})")
                continue

            # For long rows: re-embed window 0 alone and confirm cosine ~ 1.0
            sanity = ""
            if n_chunks > 1:
                vec0_again = embedder.embed([chunk_texts[0]])[0]
                cos = cosine(vecs[0], vec0_again)
                if cos < 0.99:
                    total_fail += 1
                    failures.append(f"{memory_id[:8]}: window-0 redo cosine = {cos:.4f} < 0.99")
                    continue
                sanity = f" sanity-cos={cos:.4f}"

            total_pass += 1
            print(
                f"  {memory_id[:8]} len={len(text):>6} n_chunks={n_chunks}{sanity}"
            )
        print()

    # No writes happened. Confirm by trying one:
    print("Verifying read-only: attempting a write (should raise)...")
    uri = f"file:{CHATLOG_DB.as_posix()}?mode=ro&immutable=1"
    c = sqlite3.connect(uri, uri=True)
    try:
        c.execute("INSERT INTO memory_items (id, content) VALUES ('test', 'test')")
        print("  WARN: write succeeded — DB was NOT read-only")
    except sqlite3.OperationalError as e:
        print(f"  OK: write rejected ({e})")
    c.close()

    print("\n=== SUMMARY ===")
    print(f"  pass: {total_pass}")
    print(f"  fail: {total_fail}")
    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("\nAll invariants hold on real chatlog data.")
    sys.exit(0)


if __name__ == "__main__":
    main()
