"""Read-only entity-extraction behavior regression baseline.

Snapshots `entity_search_impl` and `entity_get_impl` outputs for a
deterministic sample of canonical names + entity ids from the live
`agent_memory.db`. Required BEFORE the Phase 6 extraction of the
14 entity functions from `bin/memory_core.py` into `bin/memory/entity.py`
— see `docs/MEMORY_ENTITY_EXTRACTION_PLAN.md` and lesson #6 in
`docs/MEMORY_CORE_MODULARIZATION_LESSONS.md` ("build the behavior
baseline BEFORE the extraction, not after").

## What it captures

For each of N_QUERIES deterministically-sampled rows from the `entities`
table:

  - `entity_search_impl(query=<canonical_name>, k=10)`
  - `entity_search_impl(query=<canonical_name>, entity_type=<type>, k=10)`
  - `entity_get_impl(entity_id=<id>, depth=1)`

Each result is fingerprinted as a stable structured payload:

  - For `entity_search_impl`: list of (id, canonical_name, entity_type)
    tuples, hashed via sha256. ID-set drift = real bug.
  - For `entity_get_impl`: the entity row's id + canonical_name +
    entity_type + sorted neighbor ids + sorted memory-link ids. Drift
    here means linking or relationship traversal changed.

Score-like fields and `attributes_json` blobs are normalized (the
former is rounded; the latter is parsed-and-resorted) so floating-point
noise and dict iteration order can't cause false-positive drift.

## Workflow

  # Capture baseline before refactor (or first run)
  M3_ENTITY_REFRESH_BASELINE=1 python tests/capture_entity_baseline.py

  # Compare current behavior to baseline (default)
  python tests/capture_entity_baseline.py

Baseline lives at `.scratch/migration_baseline/entity_baseline.json`
(gitignored under .scratch/).

Exit 0 if results match baseline, 1 if any drift detected (printed in
detail).

## Out-of-scope (intentionally)

  - `_run_entity_extractor` — calls an SLM, results non-deterministic.
    Phase 6.1 will rely on parity-of-signatures + targeted unit tests
    instead of an end-to-end baseline for that function.
  - `extract_pending_impl` — mutates the queue; can't be in a read-only
    baseline. Same approach: signature parity + a manual smoke run.
  - `entity_extractor_health` — pure stats from the queue; covered by
    a single direct-call check at the end of this script.
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
BASELINE_PATH = BASELINE_DIR / "entity_baseline.json"
sys.path.insert(0, str(BIN_DIR))

MAIN_DB = ROOT / "memory" / "agent_memory.db"

# ── Sampling knobs ───────────────────────────────────────────────────────────
# 30 entities × 3 variants per entity = 90 calls total. Each `entity_search`
# is a single SQL hit (no embedding, no SLM); `entity_get` is one row +
# neighbor pull. Total wall-time ~2-3 sec on the 5080.
SEED = 1729  # Ramanujan; stable for the life of this test
N_ENTITIES = 30
SEARCH_K = 10
# ─────────────────────────────────────────────────────────────────────────────


def sample_entities(n: int) -> list[tuple[str, str, str]]:
    """Deterministic sample of (id, canonical_name, entity_type) triples.

    Sort by id and seed `random.sample` so the same rows always surface
    across runs even as the entities table grows. Read-only mode so an
    accidental write would raise instead of silently mutating the test
    corpus.
    """
    uri = f"file:{MAIN_DB.as_posix()}?mode=ro&immutable=1"
    c = sqlite3.connect(uri, uri=True)
    cur = c.cursor()
    # Pull a deterministic candidate set — ORDER BY id keeps the sample
    # stable across runs. Cap at 2000 candidates so the seeded sample
    # picks from a fixed prefix even as the table grows past that.
    cur.execute(
        """SELECT id, canonical_name, entity_type FROM entities
           WHERE canonical_name IS NOT NULL AND canonical_name != ''
             AND entity_type IS NOT NULL AND entity_type != ''
           ORDER BY id LIMIT 2000"""
    )
    cands = cur.fetchall()
    c.close()
    rng = random.Random(SEED)
    return rng.sample(cands, min(n, len(cands)))


def _normalize_entity_row(row: dict) -> dict:
    """Strip volatile fields and sort-stabilize the rest.

    `created_at` / `updated_at` / `last_accessed_at` / `access_count` are
    operational metadata that change with every access — they can't be in
    a parity hash. `attributes` (JSON-decoded) is parsed and re-serialized
    with sort_keys=True so dict-iteration order can't drift the hash.
    """
    keep = {
        "id": row.get("id"),
        "canonical_name": row.get("canonical_name"),
        "entity_type": row.get("entity_type"),
    }
    attrs = row.get("attributes") or row.get("attributes_json")
    if isinstance(attrs, str) and attrs:
        try:
            attrs = json.loads(attrs)
        except json.JSONDecodeError:
            attrs = {"__unparseable__": True}
    if isinstance(attrs, dict):
        keep["attributes"] = json.dumps(attrs, sort_keys=True)
    return keep


def fingerprint_search(rows: list[dict]) -> dict:
    """Stable structured snapshot of a search result.

    `ids` alone catches set-drift; the sha256 is the single-line yes/no
    indicator. We capture canonical_name + entity_type alongside the id
    so a drift report is human-readable without a second DB lookup.
    """
    ids = []
    triples = []
    for row in rows[:SEARCH_K]:
        if not isinstance(row, dict):
            continue
        rid = row.get("id")
        ids.append(rid)
        triples.append([rid, row.get("canonical_name"), row.get("entity_type")])
    payload = json.dumps({"triples": triples}, sort_keys=True)
    return {
        "ids": ids,
        "triples": triples,
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
    }


def fingerprint_get(result: dict) -> dict:
    """Stable structured snapshot of `entity_get_impl`'s output.

    Captures the entity row itself (normalized) plus sorted neighbor ids
    and sorted memory-link ids. Order of neighbors / links in the raw
    response is not contractually stable (Python dict iteration changes
    are unlikely but theoretically possible), so we sort before hashing.
    """
    if not isinstance(result, dict):
        return {"error": f"non-dict result: {type(result).__name__}"}
    entity = _normalize_entity_row(result.get("entity") or result)
    neighbors = result.get("neighbors") or []
    neighbor_ids = sorted(
        n.get("id") if isinstance(n, dict) else None for n in neighbors
    )
    memory_links = result.get("memory_links") or result.get("memories") or []
    link_ids = sorted(
        m.get("id") if isinstance(m, dict) else None for m in memory_links
    )
    payload = json.dumps(
        {"entity": entity, "neighbor_ids": neighbor_ids, "memory_link_ids": link_ids},
        sort_keys=True,
    )
    return {
        "entity": entity,
        "neighbor_ids": neighbor_ids,
        "memory_link_ids": link_ids,
        "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
    }


def capture_one(mc, eid: str, name: str, etype: str) -> dict:
    """Run all three variants for one entity. Errors are captured as
    `{"error": str(e)}` per-variant so one bad row doesn't kill the run."""
    out: dict = {"id": eid, "canonical_name": name[:80], "entity_type": etype}

    # Variant 1: search by canonical name only (no type filter)
    try:
        rows = mc.entity_search_impl(query=name, limit=SEARCH_K)
        out["search_by_name"] = fingerprint_search(rows or [])
    except Exception as e:
        out["search_by_name"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}

    # Variant 2: search by canonical name AND type
    try:
        rows = mc.entity_search_impl(query=name, entity_type=etype, limit=SEARCH_K)
        out["search_by_name_type"] = fingerprint_search(rows or [])
    except Exception as e:
        out["search_by_name_type"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}

    # Variant 3: get the entity with depth-1 expansion
    try:
        result = mc.entity_get_impl(entity_id=eid, depth=1)
        out["get_depth1"] = fingerprint_get(result)
    except Exception as e:
        out["get_depth1"] = {"error": f"{type(e).__name__}: {str(e)[:200]}"}

    return out


def capture() -> dict:
    """Run the full sweep. Returns the full baseline payload."""
    import memory_core as mc
    sample = sample_entities(N_ENTITIES)
    print(f"running {len(sample)} entities × 3 variants...")
    out: list[dict] = []
    t0 = time.perf_counter()
    for i, (eid, name, etype) in enumerate(sample):
        out.append(capture_one(mc, eid, name, etype))
        if (i + 1) % 10 == 0:
            elapsed = time.perf_counter() - t0
            print(
                f"  {i+1}/{len(sample)} done in {elapsed:.1f}s "
                f"({(i+1)/elapsed:.1f}/s)",
                flush=True,
            )

    # Health check — a single direct call, fingerprinted by key presence
    # (values are operational counts that drift legitimately).
    try:
        h = mc.entity_extractor_health()
        health_keys = sorted(list(h.keys())) if isinstance(h, dict) else None
    except Exception as e:
        health_keys = f"error: {type(e).__name__}"

    elapsed = time.perf_counter() - t0
    print(f"  done: {len(sample)} entities × 3 variants in {elapsed:.1f}s")
    return {
        "seed": SEED,
        "n_entities": len(sample),
        "search_k": SEARCH_K,
        "elapsed_s": round(elapsed, 2),
        "health_shape": health_keys,
        "results": out,
    }


def compare(current: dict, baseline: dict) -> tuple[bool, list[str]]:
    """Returns (matched, drift_lines). drift_lines is a human-readable list
    of every (entity × variant) that diverged with both fingerprints."""
    drift: list[str] = []
    if current["seed"] != baseline["seed"]:
        drift.append(
            f"  SEED MISMATCH — current={current['seed']} baseline={baseline['seed']}; "
            f"sample differs across runs, comparison is invalid"
        )
        return False, drift
    if current["n_entities"] != baseline["n_entities"]:
        drift.append(
            f"  N_ENTITIES MISMATCH — current={current['n_entities']} "
            f"baseline={baseline['n_entities']}"
        )

    if current.get("health_shape") != baseline.get("health_shape"):
        drift.append(
            f"  entity_extractor_health KEY-SHAPE drift — "
            f"current={current.get('health_shape')!r} "
            f"baseline={baseline.get('health_shape')!r}"
        )

    cur_results = current["results"]
    base_results = baseline["results"]

    for i, (c, b) in enumerate(zip(cur_results, base_results)):
        if c.get("id") != b.get("id"):
            drift.append(
                f"  [{i:>2}] ENTITY ID MISMATCH — current={c.get('id')} "
                f"baseline={b.get('id')}"
            )
            continue
        for variant in ("search_by_name", "search_by_name_type", "get_depth1"):
            c_fp = c.get(variant, {})
            b_fp = b.get(variant, {})
            if c_fp.get("sha256") == b_fp.get("sha256"):
                continue
            drift.append(
                f"  [{i:>2}] {variant:<22} drift "
                f"name={c.get('canonical_name', '')[:50]!r} type={c.get('entity_type')}\n"
                f"      baseline sha: {b_fp.get('sha256')}\n"
                f"      current  sha: {c_fp.get('sha256')}"
            )

    return (len(drift) == 0), drift


async def main():
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    refresh = bool(os.environ.get("M3_ENTITY_REFRESH_BASELINE"))

    if refresh or not BASELINE_PATH.exists():
        if not BASELINE_PATH.exists():
            print(f"No baseline at {BASELINE_PATH} — capturing fresh.")
        else:
            print("M3_ENTITY_REFRESH_BASELINE set — overwriting baseline.")
        current = capture()
        BASELINE_PATH.write_text(json.dumps(current, indent=2, sort_keys=True))
        print(f"  wrote baseline: {BASELINE_PATH}")
        print("  re-run without M3_ENTITY_REFRESH_BASELINE to compare.")
        sys.exit(0)

    print(f"Comparing against baseline: {BASELINE_PATH}")
    baseline = json.loads(BASELINE_PATH.read_text())
    print(
        f"  baseline: seed={baseline['seed']} n_entities={baseline['n_entities']} "
        f"search_k={baseline['search_k']}"
    )
    current = capture()
    matched, drift_lines = compare(current, baseline)

    if matched:
        print("\nOK — entity-side output is byte-identical to baseline.")
        sys.exit(0)
    else:
        print(f"\nDRIFT DETECTED — {len(drift_lines)} variant(s) diverged:")
        for line in drift_lines:
            print(line)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
