"""Integration sweep for sliding-window chunking against real chatlog data,
N=2000 rows. READ-ONLY (mode=ro&immutable=1).

For each row, records:
  - memory_id
  - raw_chars (= len(content))
  - aug_chars (= len(augmented embed text); approx == raw_chars without metadata)
  - n_chunks
  - chunk_chars[i] for i in 0..n_chunks
  - approx_tokens_per_chunk[i] (= chars // 3.5)
  - embed_ok per chunk
  - embed_err per chunk (if any)
  - row_embed_seconds

Aggregates printed at the end:
  - count by bucket (single-chunk vs multi-chunk; embed_ok vs partial vs all_fail)
  - chunk-count distribution
  - chunk-char-length distribution
  - approx-token distribution
  - dense-overflow count (chunks that tokenize > 8192 despite being under MAX chars)
  - sample failure summaries

Writes a CSV at .scratch/integration_chunking_2k.csv.
"""
from __future__ import annotations

import csv
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

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

import memory_core as mc  # noqa: E402
import m3_core_rs  # noqa: E402

CHATLOG_DB = Path(r"C:\Users\bhaba\m3-memory\memory\agent_chatlog.db")
CSV_OUT = Path(r"C:\Users\bhaba\m3-memory\.scratch\integration_chunking_2k.csv")
N_TOTAL = 2000
PROGRESS_EVERY = 100


def sample_rows(n: int):
    """Random sample N rows. Read-only DB connect."""
    uri = f"file:{CHATLOG_DB.as_posix()}?mode=ro&immutable=1"
    c = sqlite3.connect(uri, uri=True)
    cur = c.cursor()
    cur.execute(
        """SELECT id, content, metadata_json FROM memory_items
           WHERE is_deleted = 0 AND COALESCE(content, '') != ''
           ORDER BY RANDOM() LIMIT ?""",
        (n,),
    )
    rows = cur.fetchall()
    c.close()
    return rows


def main():
    print(f"Config: MAX={mc.MAX_CHARS_PER_CHUNK} OVL={mc.MIN_OVERLAP_CHARS} STRIDE={mc.STRIDE_CHARS}")
    print(f"DB (read-only): {CHATLOG_DB}")
    print(f"Sampling {N_TOTAL} rows...\n")
    rows = sample_rows(N_TOTAL)
    print(f"sampled: {len(rows)} rows\n")

    print("Loading bge-m3 in-process embedder...")
    t0 = time.perf_counter()
    embedder = m3_core_rs.EmbeddedEmbedder(os.environ["M3_EMBED_GGUF"])
    dim = embedder.embedding_dim()
    print(f"  loaded in {time.perf_counter()-t0:.1f}s, dim={dim}, backend={m3_core_rs.embed_backend_label()}\n")
    assert dim == 1024

    CSV_OUT.parent.mkdir(parents=True, exist_ok=True)
    csv_f = CSV_OUT.open("w", newline="", encoding="utf-8")
    csv_w = csv.writer(csv_f)
    csv_w.writerow([
        "memory_id", "raw_chars", "aug_chars", "n_chunks",
        "chunk_chars", "approx_tokens", "embed_ok_per_chunk",
        "row_embed_s", "errors",
    ])

    counters = Counter()
    n_chunks_hist = Counter()
    chunk_char_hist = Counter()
    chunk_tok_hist = Counter()
    dense_overflow_rows = []
    embed_failures = []
    total_chunks = 0
    total_chunks_embedded_ok = 0
    total_embed_s = 0.0

    t_run = time.perf_counter()
    for i, (mid, content, meta_json) in enumerate(rows):
        raw_chars = len(content or "")
        aug = mc._augment_embed_text_with_anchors(content or "", meta_json)
        aug_chars = len(aug)
        chunks = mc._chunk_for_sliding_window(aug)
        n_chunks = len(chunks)
        chunk_texts = [ct for ct, _ in chunks]
        chunk_chars = [len(ct) for ct in chunk_texts]
        approx_tokens = [c // 4 for c in chunk_chars]  # ~4 char/tok English; ratio varies

        # Histogram bucketing
        n_chunks_hist[n_chunks] += 1
        for c in chunk_chars:
            # Bucket by 5000-char bins
            chunk_char_hist[(c // 5000) * 5000] += 1
        for t in approx_tokens:
            chunk_tok_hist[(t // 1000) * 1000] += 1

        # Embed all chunks
        t_e = time.perf_counter()
        per_chunk_ok = []
        row_errors = []
        try:
            vecs = embedder.embed(chunk_texts)
            for j, v in enumerate(vecs):
                if v and len(v) == dim:
                    per_chunk_ok.append(True)
                    total_chunks_embedded_ok += 1
                else:
                    per_chunk_ok.append(False)
                    row_errors.append(f"chunk{j}: bad vec dim {len(v) if v else 'None'}")
        except Exception as e:
            err = str(e)
            row_errors.append(err)
            per_chunk_ok = [False] * n_chunks
            # Check for the dense-overflow signature
            if "input too long" in err and "tokens" in err:
                dense_overflow_rows.append((mid, raw_chars, n_chunks, err))
            else:
                embed_failures.append((mid, raw_chars, err))

        row_s = time.perf_counter() - t_e
        total_embed_s += row_s
        total_chunks += n_chunks

        # Categorize the row
        if n_chunks == 1:
            counters["single_chunk"] += 1
        else:
            counters["multi_chunk"] += 1
        if all(per_chunk_ok):
            counters["all_chunks_ok"] += 1
        elif any(per_chunk_ok):
            counters["partial_ok"] += 1
        else:
            counters["all_fail"] += 1

        csv_w.writerow([
            mid, raw_chars, aug_chars, n_chunks,
            ";".join(str(c) for c in chunk_chars),
            ";".join(str(t) for t in approx_tokens),
            ";".join("1" if ok else "0" for ok in per_chunk_ok),
            f"{row_s:.3f}",
            " | ".join(row_errors) if row_errors else "",
        ])

        if (i + 1) % PROGRESS_EVERY == 0:
            elapsed = time.perf_counter() - t_run
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (N_TOTAL - i - 1) / rate if rate > 0 else 0
            print(
                f"  {i+1}/{N_TOTAL} rows  chunks_total={total_chunks}  "
                f"chunks_ok={total_chunks_embedded_ok}  "
                f"row_rate={rate:.1f}/s  eta={eta:.0f}s",
                flush=True,
            )

    csv_f.close()
    total_wall = time.perf_counter() - t_run

    # =============== Report ===============
    print(f"\n=== TIMING ===")
    print(f"  total wall: {total_wall:.1f}s")
    print(f"  total embed time: {total_embed_s:.1f}s")
    print(f"  rows/sec: {N_TOTAL/total_wall:.1f}")
    print(f"  chunks/sec: {total_chunks/total_wall:.1f}")
    print(f"  total chunks: {total_chunks}")
    print(f"  chunks/row avg: {total_chunks/N_TOTAL:.2f}")

    print(f"\n=== ROW BUCKETS ===")
    print(f"  single_chunk rows: {counters['single_chunk']}  ({100*counters['single_chunk']/N_TOTAL:.1f}%)")
    print(f"  multi_chunk rows:  {counters['multi_chunk']}  ({100*counters['multi_chunk']/N_TOTAL:.1f}%)")
    print(f"  all_chunks_ok:     {counters['all_chunks_ok']}  ({100*counters['all_chunks_ok']/N_TOTAL:.1f}%)")
    print(f"  partial_ok:        {counters['partial_ok']}")
    print(f"  all_fail:          {counters['all_fail']}")

    print(f"\n=== n_chunks distribution ===")
    for nc in sorted(n_chunks_hist):
        print(f"  n_chunks={nc:>3}: {n_chunks_hist[nc]:>4} rows  ({100*n_chunks_hist[nc]/N_TOTAL:.1f}%)")

    print(f"\n=== chunk char-length distribution (5000-char bins) ===")
    for bucket in sorted(chunk_char_hist):
        print(f"  [{bucket:>6}, {bucket+5000:>6}): {chunk_char_hist[bucket]:>5} chunks")

    print(f"\n=== chunk approx-token distribution (1000-token bins, chars/4) ===")
    for bucket in sorted(chunk_tok_hist):
        print(f"  [{bucket:>5}, {bucket+1000:>5}): {chunk_tok_hist[bucket]:>5} chunks")

    print(f"\n=== DENSE OVERFLOW (chunks tokenized > 8192 despite < {mc.MAX_CHARS_PER_CHUNK} chars) ===")
    print(f"  count: {len(dense_overflow_rows)} rows")
    for mid, rc, nc, err in dense_overflow_rows[:10]:
        print(f"  {mid[:8]} raw_chars={rc} n_chunks={nc}  err: {err[:120]}")
    if len(dense_overflow_rows) > 10:
        print(f"  ... and {len(dense_overflow_rows)-10} more")

    print(f"\n=== OTHER EMBED FAILURES ===")
    print(f"  count: {len(embed_failures)} rows")
    for mid, rc, err in embed_failures[:10]:
        print(f"  {mid[:8]} raw_chars={rc}  err: {err[:120]}")

    print(f"\nCSV written: {CSV_OUT}")

    # Read-only proof
    print("\nVerifying read-only DB (attempting INSERT)...")
    uri = f"file:{CHATLOG_DB.as_posix()}?mode=ro&immutable=1"
    cc = sqlite3.connect(uri, uri=True)
    try:
        cc.execute("INSERT INTO memory_items (id, content) VALUES ('test', 'test')")
        print("  WARN: DB was NOT read-only")
    except sqlite3.OperationalError as e:
        print(f"  OK: write rejected ({e})")
    cc.close()


if __name__ == "__main__":
    main()
