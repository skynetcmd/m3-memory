"""
Build a KG-enriched variant from an existing source variant.

Duplicates memory_items + memory_embeddings under a new variant name (fresh IDs),
then populates memory_relationships with `related` edges computed from cosine
similarity on the duplicated embeddings. No LLM calls, no re-ingest.

Usage:
    python bin/build_kg_variant.py \
        --source-variant LME-ingestion \
        --target-variant LME-kg-sparse \
        --top-n 3 --sim-threshold 0.80

    python bin/build_kg_variant.py \
        --source-variant LME-ingestion \
        --target-variant LME-kg-dense \
        --top-n 8 --sim-threshold 0.70
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import struct
import sys
import time
import uuid
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from m3_sdk import add_database_arg, resolve_db_path

# AGENT_DB env var is kept as a deprecated alias; new code should prefer
# M3_DATABASE or the --database CLI flag.
_AGENT_DB_LEGACY = os.environ.get("AGENT_DB")
DB_PATH = _AGENT_DB_LEGACY or resolve_db_path(None)


def _unpack(blob: bytes) -> np.ndarray:
    n = len(blob) // 4
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def copy_items_and_embeddings(db: sqlite3.Connection, source: str, target: str) -> dict[str, str]:
    """Duplicate items and embeddings from source variant to target variant with new IDs.
    Returns old_id -> new_id mapping."""
    log(f"copying items from variant={source!r} to variant={target!r}")
    # Ensure target is empty
    existing = db.execute("SELECT COUNT(*) FROM memory_items WHERE variant = ?", (target,)).fetchone()[0]
    if existing:
        raise SystemExit(f"target variant {target!r} already has {existing} items — aborting. Use --wipe-target to clear.")

    rows = db.execute(
        "SELECT id, type, title, content, metadata_json, agent_id, model_id, "
        "change_agent, importance, source, origin_device, is_deleted, expires_at, "
        "decay_rate, created_at, updated_at, last_accessed_at, access_count, user_id, "
        "scope, valid_from, valid_to, content_hash, read_at, conversation_id, "
        "refresh_on, refresh_reason FROM memory_items WHERE variant = ? AND is_deleted = 0",
        (source,),
    ).fetchall()
    log(f"  {len(rows)} source items to copy")

    id_map: dict[str, str] = {}
    now = datetime.now(timezone.utc).isoformat()
    batch = []
    for r in rows:
        new_id = str(uuid.uuid4())
        id_map[r[0]] = new_id
        batch.append((
            new_id, r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[10],
            r[11], r[12], r[13], r[14], now, r[16], r[17], r[18], r[19], r[20],
            r[21], r[22], r[23], r[24], r[25], r[26], target,
        ))
    db.executemany(
        "INSERT INTO memory_items (id, type, title, content, metadata_json, agent_id, "
        "model_id, change_agent, importance, source, origin_device, is_deleted, "
        "expires_at, decay_rate, created_at, updated_at, last_accessed_at, "
        "access_count, user_id, scope, valid_from, valid_to, content_hash, read_at, "
        "conversation_id, refresh_on, refresh_reason, variant) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        batch,
    )
    db.commit()
    log(f"  inserted {len(batch)} items")

    log("copying embeddings")
    emb_batch = []
    for old_id, new_id in id_map.items():
        er = db.execute(
            "SELECT embedding, embed_model, dim, content_hash FROM memory_embeddings "
            "WHERE memory_id = ? LIMIT 1",
            (old_id,),
        ).fetchone()
        if er is None:
            continue
        emb_batch.append((str(uuid.uuid4()), new_id, er[0], er[1], er[2], now, er[3]))
    db.executemany(
        "INSERT INTO memory_embeddings (id, memory_id, embedding, embed_model, dim, created_at, content_hash) "
        "VALUES (?,?,?,?,?,?,?)",
        emb_batch,
    )
    db.commit()
    log(f"  inserted {len(emb_batch)} embeddings")
    return id_map


def build_kg_edges(db: sqlite3.Connection, variant: str, top_n: int, sim_threshold: float) -> int:
    """For each item in variant, find top-N similar items above threshold and insert
    `related` edges into memory_relationships."""
    log(f"building KG edges for variant={variant!r} (top_n={top_n}, threshold={sim_threshold})")
    rows = db.execute(
        "SELECT mi.id, me.embedding FROM memory_items mi "
        "JOIN memory_embeddings me ON mi.id = me.memory_id "
        "WHERE mi.variant = ? AND mi.is_deleted = 0",
        (variant,),
    ).fetchall()
    log(f"  {len(rows)} items with embeddings")
    if not rows:
        return 0

    ids = np.array([r[0] for r in rows])
    vecs = np.stack([_unpack(r[1]) for r in rows])  # (N, 1024)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    vecs_norm = vecs / norms
    log(f"  vector matrix shape={vecs_norm.shape}, computing similarity in chunks")

    chunk_size = 512  # 512 x 353K float32 sim matrix = ~720 MB per chunk
    now = datetime.now(timezone.utc).isoformat()
    total_edges = 0
    t0 = time.perf_counter()

    # Use WAL for bulk insert perf
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")

    for start in range(0, len(vecs_norm), chunk_size):
        end = min(start + chunk_size, len(vecs_norm))
        chunk = vecs_norm[start:end]
        sims = chunk @ vecs_norm.T  # (chunk, N)
        # Zero out self-similarity
        for i in range(end - start):
            sims[i, start + i] = -1.0

        edges = []
        for i in range(end - start):
            row_sims = sims[i]
            # Indices of top-N above threshold
            candidate_mask = row_sims >= sim_threshold
            if not candidate_mask.any():
                continue
            candidate_idxs = np.where(candidate_mask)[0]
            if len(candidate_idxs) > top_n:
                top_local = np.argpartition(-row_sims[candidate_idxs], top_n)[:top_n]
                candidate_idxs = candidate_idxs[top_local]
            from_id = ids[start + i]
            for j in candidate_idxs:
                edges.append((str(uuid.uuid4()), str(from_id), str(ids[j]), "related", now))

        if edges:
            db.executemany(
                "INSERT INTO memory_relationships (id, from_id, to_id, relationship_type, created_at) "
                "VALUES (?,?,?,?,?)",
                edges,
            )
            db.commit()
            total_edges += len(edges)

        if (end // chunk_size) % 20 == 0 or end == len(vecs_norm):
            elapsed = time.perf_counter() - t0
            rate = end / elapsed if elapsed else 0
            log(f"  processed {end}/{len(vecs_norm)} ({rate:.0f}/s), edges so far: {total_edges}")

    log(f"  done: {total_edges} edges inserted in {time.perf_counter()-t0:.1f}s")
    return total_edges


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--source-variant", required=True)
    p.add_argument("--target-variant", required=True)
    p.add_argument("--top-n", type=int, required=True)
    p.add_argument("--sim-threshold", type=float, required=True)
    p.add_argument("--wipe-target", action="store_true",
                   help="Delete any existing items/edges under target variant before building")
    add_database_arg(p)
    args = p.parse_args()

    db_path = resolve_db_path(args.database) if args.database else DB_PATH
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    if args.wipe_target:
        log(f"wiping existing target variant {args.target_variant!r}")
        # Delete edges referencing items in target variant
        db.execute(
            "DELETE FROM memory_relationships WHERE from_id IN "
            "(SELECT id FROM memory_items WHERE variant = ?) OR to_id IN "
            "(SELECT id FROM memory_items WHERE variant = ?)",
            (args.target_variant, args.target_variant),
        )
        db.execute("DELETE FROM memory_embeddings WHERE memory_id IN "
                   "(SELECT id FROM memory_items WHERE variant = ?)",
                   (args.target_variant,))
        db.execute("DELETE FROM memory_items WHERE variant = ?", (args.target_variant,))
        db.commit()

    copy_items_and_embeddings(db, args.source_variant, args.target_variant)
    n_edges = build_kg_edges(db, args.target_variant, args.top_n, args.sim_threshold)

    # Summary
    n_items = db.execute(
        "SELECT COUNT(*) FROM memory_items WHERE variant = ?", (args.target_variant,)
    ).fetchone()[0]
    log(f"SUMMARY: variant={args.target_variant} items={n_items} edges={n_edges}")


if __name__ == "__main__":
    main()
