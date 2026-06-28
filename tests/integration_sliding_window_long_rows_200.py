"""Curated long-row sweep: 200 rows with LENGTH(content) > MAX_CHARS_PER_CHUNK.

READ-ONLY. Exercises the sliding-window chunking path (the random sample
showed 0% multi-chunk rows; this filter guarantees 100% multi-chunk).

Records per-row chunk count, chunk char/token sizes, embed success, dense
overflow incidents. Quantifies how often dense content trips the 8192-token
ceiling despite the conservative 28000-char limit.
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
    r"C:\Users\username\.lmstudio\models\deepsweet\bge-m3-GGUF-Q4_K_M\bge-m3-GGUF-Q4_K_M.gguf",
)
os.environ.setdefault("M3_EMBED_STREAMS", "1")
os.environ.setdefault("M3_EMBED_CTX", "8192")
os.environ.setdefault("M3_EMBED_SEQ_MAX", "8")
os.environ.setdefault("M3_EMBED_N_BATCH", "8192")
os.environ.setdefault("M3_EMBED_N_UBATCH", "8192")

import m3_core_rs  # noqa: E402
import memory_core as mc  # noqa: E402

DBS = [
    Path(r"C:\Users\username\m3-memory\memory\agent_chatlog.db"),
    Path(r"C:\Users\username\m3-memory\memory\agent_memory.db"),
]
CSV_OUT = Path(r"C:\Users\username\m3-memory\.scratch\integration_chunking_long_200.csv")
N_TOTAL_PER_DB = 200  # cap; we take min(corpus-long-rows, this) per DB
PROGRESS_EVERY = 25


def sample_long_rows_from(db_path: Path, n: int):
    """Sample up to n long rows from one DB. Read-only."""
    uri = f"file:{db_path.as_posix()}?mode=ro&immutable=1"
    c = sqlite3.connect(uri, uri=True)
    cur = c.cursor()
    cur.execute(
        """SELECT id, content, metadata_json FROM memory_items
           WHERE is_deleted = 0
             AND LENGTH(content) > ?
           ORDER BY RANDOM() LIMIT ?""",
        (mc.MAX_CHARS_PER_CHUNK, n),
    )
    rows = [(db_path.name, r[0], r[1], r[2]) for r in cur.fetchall()]
    c.close()
    return rows


def sample_long_rows(n_per_db: int):
    """Sample from both DBs, return combined list with db_label."""
    all_rows = []
    for db in DBS:
        if not db.exists():
            print(f"  WARN: {db} does not exist, skipping")
            continue
        rows = sample_long_rows_from(db, n_per_db)
        print(f"  {db.name}: found {len(rows)} long rows")
        all_rows.extend(rows)
    return all_rows


def main():
    print(f"Config: MAX={mc.MAX_CHARS_PER_CHUNK} OVL={mc.MIN_OVERLAP_CHARS} STRIDE={mc.STRIDE_CHARS}")
    print("DBs (read-only):")
    for db in DBS:
        print(f"  - {db}")
    print(f"\nSampling up to {N_TOTAL_PER_DB} long rows from each DB (LENGTH(content) > {mc.MAX_CHARS_PER_CHUNK})...")
    rows = sample_long_rows(N_TOTAL_PER_DB)
    print(f"\ntotal sampled: {len(rows)} long rows across both DBs")
    if not rows:
        print("No long rows in either corpus — nothing to test.")
        return
    print()

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
        "db", "memory_id", "raw_chars", "aug_chars", "n_chunks",
        "chunk_chars", "approx_tokens", "embed_ok_per_chunk",
        "row_embed_s", "errors",
    ])

    counters = Counter()
    n_chunks_hist = Counter()
    chunk_char_hist = Counter()
    chunk_tok_hist = Counter()
    dense_overflow_rows = []
    failed_chunk_token_counts = []
    other_failures = []
    total_chunks = 0
    total_chunks_embedded_ok = 0

    per_db_counters: dict[str, Counter] = {}
    t_run = time.perf_counter()
    for i, (db_label, mid, content, meta_json) in enumerate(rows):
        if db_label not in per_db_counters:
            per_db_counters[db_label] = Counter()
        raw_chars = len(content or "")
        aug = mc._augment_embed_text_with_anchors(content or "", meta_json)
        aug_chars = len(aug)
        chunks = mc._chunk_for_sliding_window(aug)
        n_chunks = len(chunks)
        chunk_texts = [ct for ct, _ in chunks]
        chunk_chars = [len(ct) for ct in chunk_texts]
        approx_tokens = [c // 4 for c in chunk_chars]

        n_chunks_hist[n_chunks] += 1
        for c in chunk_chars:
            chunk_char_hist[(c // 5000) * 5000] += 1
        for t in approx_tokens:
            chunk_tok_hist[(t // 1000) * 1000] += 1

        # Embed chunk-by-chunk so we know exactly which chunk fails (the
        # batched embed call aborts the whole batch on first failure).
        t_e = time.perf_counter()
        per_chunk_ok = []
        row_errors = []
        for j, ct in enumerate(chunk_texts):
            try:
                v = embedder.embed([ct])[0]
                if v and len(v) == dim:
                    per_chunk_ok.append(True)
                    total_chunks_embedded_ok += 1
                else:
                    per_chunk_ok.append(False)
                    row_errors.append(f"chunk{j}: bad vec dim {len(v) if v else 'None'}")
            except Exception as e:
                err = str(e)
                per_chunk_ok.append(False)
                row_errors.append(f"chunk{j}: {err}")
                if "input too long" in err and "tokens" in err:
                    # llama.cpp error format: "input too long: NNNN tokens > n_ctx 8192"
                    # Extract NNNN robustly.
                    tok = None
                    import re
                    m = re.search(r"input too long:\s*(\d+)\s*tokens", err)
                    if m:
                        try:
                            tok = int(m.group(1))
                        except Exception:
                            pass
                    dense_overflow_rows.append({
                        "db": db_label,
                        "memory_id": mid,
                        "raw_chars": raw_chars,
                        "n_chunks": n_chunks,
                        "chunk_idx": j,
                        "chunk_chars": chunk_chars[j],
                        "reported_tokens": tok,
                        "chars_per_token": (chunk_chars[j] / tok) if tok else None,
                        "err": err,
                    })
                    failed_chunk_token_counts.append(tok if tok else 0)
                else:
                    other_failures.append({"db": db_label, "memory_id": mid, "err": err})

        row_s = time.perf_counter() - t_e
        total_chunks += n_chunks

        if n_chunks == 1:
            counters["single_chunk"] += 1
            per_db_counters[db_label]["single_chunk"] += 1
        else:
            counters["multi_chunk"] += 1
            per_db_counters[db_label]["multi_chunk"] += 1
        if all(per_chunk_ok):
            counters["all_chunks_ok"] += 1
            per_db_counters[db_label]["all_chunks_ok"] += 1
        elif any(per_chunk_ok):
            counters["partial_ok"] += 1
            per_db_counters[db_label]["partial_ok"] += 1
        else:
            counters["all_fail"] += 1
            per_db_counters[db_label]["all_fail"] += 1
        per_db_counters[db_label]["rows"] += 1

        csv_w.writerow([
            db_label, mid, raw_chars, aug_chars, n_chunks,
            ";".join(str(c) for c in chunk_chars),
            ";".join(str(t) for t in approx_tokens),
            ";".join("1" if ok else "0" for ok in per_chunk_ok),
            f"{row_s:.3f}",
            " | ".join(row_errors) if row_errors else "",
        ])

        if (i + 1) % PROGRESS_EVERY == 0:
            elapsed = time.perf_counter() - t_run
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (len(rows) - i - 1) / rate if rate > 0 else 0
            print(
                f"  {i+1}/{len(rows)} rows  chunks_total={total_chunks}  "
                f"chunks_ok={total_chunks_embedded_ok}  "
                f"dense_overflow_chunks={len(dense_overflow_rows)}  "
                f"row_rate={rate:.1f}/s  eta={eta:.0f}s",
                flush=True,
            )

    csv_f.close()
    total_wall = time.perf_counter() - t_run
    N = len(rows)

    print("\n=== TIMING ===")
    print(f"  total wall: {total_wall:.1f}s")
    print(f"  rows/sec: {N/total_wall:.1f}")
    print(f"  chunks/sec: {total_chunks/total_wall:.1f}")
    print(f"  total chunks: {total_chunks}")
    print(f"  chunks/row avg: {total_chunks/N:.2f}")

    print(f"\n=== ROW BUCKETS (combined, N={N}) ===")
    print(f"  single_chunk: {counters['single_chunk']}  (sanity check: should be 0 — we filtered to long rows)")
    print(f"  multi_chunk:  {counters['multi_chunk']}  ({100*counters['multi_chunk']/N:.1f}%)")
    print(f"  all_chunks_ok: {counters['all_chunks_ok']}  ({100*counters['all_chunks_ok']/N:.1f}%)")
    print(f"  partial_ok:    {counters['partial_ok']}  ({100*counters['partial_ok']/N:.1f}%)")
    print(f"  all_fail:      {counters['all_fail']}  ({100*counters['all_fail']/N:.1f}%)")

    print("\n=== PER-DB BREAKDOWN ===")
    for db_label, pc in per_db_counters.items():
        n = pc["rows"]
        print(f"  {db_label} (N={n}):")
        print(f"    multi_chunk={pc['multi_chunk']} all_ok={pc['all_chunks_ok']} partial={pc['partial_ok']} all_fail={pc['all_fail']}")

    print("\n=== n_chunks distribution ===")
    for nc in sorted(n_chunks_hist):
        print(f"  n_chunks={nc:>3}: {n_chunks_hist[nc]:>4} rows  ({100*n_chunks_hist[nc]/N:.1f}%)")

    print("\n=== chunk char-length distribution (5000-char bins) ===")
    for bucket in sorted(chunk_char_hist):
        print(f"  [{bucket:>6}, {bucket+5000:>6}): {chunk_char_hist[bucket]:>5} chunks")

    print("\n=== chunk approx-token distribution (1000-token bins, chars/4) ===")
    for bucket in sorted(chunk_tok_hist):
        print(f"  [{bucket:>5}, {bucket+1000:>5}): {chunk_tok_hist[bucket]:>5} chunks")

    print("\n=== DENSE OVERFLOW (chunks tokenized > 8192) ===")
    print(f"  failed-chunk count: {len(dense_overflow_rows)}")
    print(f"  failed-row count:   {len(set(r['memory_id'] for r in dense_overflow_rows))}")
    if dense_overflow_rows:
        toks = [r["reported_tokens"] for r in dense_overflow_rows if r["reported_tokens"]]
        if toks:
            print(f"  observed token counts: min={min(toks)}, max={max(toks)}, median={sorted(toks)[len(toks)//2]}")
        ratios = [r["chars_per_token"] for r in dense_overflow_rows if r["chars_per_token"]]
        if ratios:
            print(f"  observed chars/token: min={min(ratios):.2f}, max={max(ratios):.2f}, median={sorted(ratios)[len(ratios)//2]:.2f}")
        # Sample dense-overflow rows
        print("\n  Sample of overflowing chunks (first 8):")
        for r in dense_overflow_rows[:8]:
            cpt = f"{r['chars_per_token']:.2f}" if r['chars_per_token'] else "n/a"
            print(
                f"    {r['memory_id'][:8]} raw={r['raw_chars']:>6}c "
                f"chunk[{r['chunk_idx']}]={r['chunk_chars']}c "
                f"tokens={r['reported_tokens']} chars/tok={cpt}"
            )

    print("\n=== OTHER EMBED FAILURES (non-overflow) ===")
    print(f"  count: {len(other_failures)}")
    for r in other_failures[:5]:
        print(f"    {r['memory_id'][:8]}: {r['err'][:120]}")

    # Recommendation
    print("\n=== IMPLICATION ===")
    if dense_overflow_rows:
        n_failed_rows = len(set(r['memory_id'] for r in dense_overflow_rows))
        pct = 100 * n_failed_rows / N
        print(f"  {pct:.1f}% of long rows had at least one chunk hit the dense-overflow ceiling.")
        # What MAX_CHARS would have been safe?
        if ratios:
            worst_ratio = min(ratios)
            safe_max = int(8192 * worst_ratio * 0.9)  # 10% safety margin
            print(f"  Worst chars/token observed: {worst_ratio:.2f}")
            print(f"  Recommended MAX_CHARS_PER_CHUNK for 100% safety: ~{safe_max} (10% margin under 8192-token ceiling)")
    else:
        print(f"  No dense overflow on {N} long rows. Current MAX={mc.MAX_CHARS_PER_CHUNK} is safe for this corpus.")

    print(f"\nCSV written: {CSV_OUT}")


if __name__ == "__main__":
    main()
