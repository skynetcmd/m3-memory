"""Provisional-entity coalescing pass (v1: detect + quarantine + review only).

Files fact-extraction links facts -> entities with an exact-match-or-create-
provisional policy (no fuzzy/semantic match at ingest, to keep the ingest txn
fast). That accumulates near-duplicate + non-entity "provisional" rows in the
CORE memory DB (agent_memory.db): e.g. `database`/`databases`/`DB`, plus noise
like `$0.25`, `%APPDATA%`, `#bug-reports`. This module is the post-ingest pass
that cleans them up.

v1 scope (this file): DETECTION + QUARANTINE + REVIEW QUEUE only. It NEVER
merges destructively and never auto-applies. "Coalescing" is modeled as a
reversible overlay (a `same_as` edge / shared cluster_id) decided later by a
human reviewing the queue — members stay intact, canonical view is a read-time
projection, reversal is trivial. (Design + lessons in
to_be_deleted/ENTITY_COALESCING_PASS_SCOPE.md.)

Pipeline:
  Stage 0  prune  -> quarantine non-entity noise (reversible flag, never delete)
  Stage 1  block  -> first-token, type-constrained; singletons drop out free
  Stage 2  score  -> rapidfuzz within block; embed ONLY unresolved survivors
  Stage 3  flag   -> write candidate pairs to entity_coalesce_candidates with a
                     band (merge / needs_llm / related-not-same is a review verdict)

Public API:
    coalesce_detect(corpus=None, ...) -> dict            # the batch pass
    list_coalesce_candidates(reviewed=False, ...) -> list[dict]
    review_coalesce_candidates([{uuid, action}], ...) -> dict   # BULK (§3)

Design philosophies honored: read-only detect (§6), indexed working-set columns
not a JSON LIKE scan (§8), reuse the pool + batch-embed (§4), cluster-growth
guardrail + type-constraint + false-split bias (lessons #2/#6/#7), structured
returns + bulk review (§3).
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
import uuid as _uuid
from typing import Optional

from .dedup import _cosine_packed
from .entities import _memory_db

logger = logging.getLogger("files_memory.entity_coalesce")

# ── Tunables (env-overridable, mirrors files_dedup) ──────────────────────────
AUTO_MERGE_COSINE: float = float(os.environ.get("M3_ENTITY_COALESCE_AUTOMERGE", "0.95"))
FLAG_COSINE: float = float(os.environ.get("M3_ENTITY_COALESCE_FLAG", "0.80"))
FUZZY_HIGH: int = int(os.environ.get("M3_ENTITY_COALESCE_FUZZY_HIGH", "90"))
MAX_PAIRS: int = int(os.environ.get("M3_ENTITY_COALESCE_MAX_PAIRS", "1000"))
MAX_BLOCK: int = int(os.environ.get("M3_ENTITY_COALESCE_MAX_BLOCK", "400"))
# Cluster-growth guardrail (lesson #2): refuse to auto-band a pair that would
# grow a same_as cluster beyond this — route to review instead.
MAX_CLUSTER: int = int(os.environ.get("M3_ENTITY_COALESCE_MAX_CLUSTER", "12"))

# Stage-0 noise: code keywords / generic nouns that are never useful entities.
_DENY_NOUNS = frozenset({
    "null", "true", "false", "none", "status", "result", "value", "default",
    "database", "table", "row", "column", "key", "id", "name", "type", "data",
    "error", "count", "max", "min", "sum", "coalesce", "select", "insert",
    "update", "delete", "where", "from", "join", "index", "query", "field",
})
# Name patterns that are clearly not entities: prices/numbers, shell/env vars,
# channel handles, quoted fragments, pure punctuation/symbol starts.
_NOISE_RE = re.compile(r"""^(?:\$|%|#|['"`]|\d|[^\w])""")


def _is_noise(name: str) -> bool:
    n = (name or "").strip()
    if not n:
        return True
    if _NOISE_RE.match(n):
        return True
    if n.lower() in _DENY_NOUNS:
        return True
    return False


def _underscore_collision(a: str, b: str) -> bool:
    """True when two names are identical EXCEPT for a leading underscore (and/or
    case) — e.g. `content_hash` vs `_content_hash`, `predicates` vs `_PREDICATES`,
    `routed_impl` vs `_routed_impl`. In m3's codebase a leading `_` distinguishes
    a PRIVATE helper from a public value/concept, so these are usually DIFFERENT
    entities. Validated on the first live v1 review: such pairs scored 0.95+/95+
    (would auto-merge) but are false merges. Demote them to needs_llm so a human/
    LLM adjudicates rather than auto-applying. (Lesson #6 — bias to false splits.)"""
    na, nb = (a or "").lower(), (b or "").lower()
    return na != nb and na.lstrip("_") == nb.lstrip("_")


# Tokenize on non-alphanumerics so `k20`, `v2`, `gpt-4`, `baseline_1` split into
# the alpha stem and the trailing number. A token like `k20` is itself split to
# (`k`, `20`) — that lets `...k20` vs `...k30` align on `k` and diverge on the
# pure-digit token, which is exactly the collision we want to catch.
_TOK_RE = re.compile(r"[a-z]+|\d+")


def _numeric_suffix_collision(a: str, b: str) -> bool:
    """True when two names are identical EXCEPT for a differing NUMERIC/version
    token — e.g. `run-config-k20` vs `...-k30`, `gpt-4` vs `gpt-5`,
    `baseline-1` vs `baseline-2`. These score 0.95+/95+ (would auto-merge) but
    are usually DISTINCT configs/versions/experiments, not the same entity.
    Validated on the first live v1 auto-merge band, which wrongly paired two
    run configs differing only in a `k20`/`k30` trailing token. Demote to
    needs_llm so a human/LLM adjudicates. (Lesson #6 — bias to false splits.)

    Returns False for singular/plural or punctuation-only differences (those are
    genuine merges, e.g. `entity row`/`entity rows`) — only a differing token
    where ALL differing positions involve a numeric token trips the guard."""
    ta, tb = _TOK_RE.findall(a.lower()), _TOK_RE.findall(b.lower())
    if ta == tb or len(ta) != len(tb):
        return False
    diffs = [(x, y) for x, y in zip(ta, tb) if x != y]
    if not diffs:
        return False
    # Every differing position must involve a numeric token on at least one side
    # AND the non-numeric stems (if any) must match — i.e. the divergence is
    # purely the number. `k20`->(`k`,`20`) handled by tokenization above.
    return all(x.isdigit() or y.isdigit() for x, y in diffs)


def _block_key(name: str) -> str:
    """Cheap blocking key: lowercased first alpha token. Singletons under this
    key have no candidate and are skipped — zero compare cost."""
    toks = re.findall(r"[a-zA-Z0-9]+", (name or "").lower())
    return toks[0] if toks else ""


# ── Schema (idempotent; runs against agent_memory.db) ────────────────────────
def ensure_schema(mem: sqlite3.Connection) -> None:
    """Additive, idempotent. Indexed working-set columns on `entities`
    (provisional/cluster_id/resolution_run) replace the unindexable
    `attributes_json LIKE '%provisional%'` scan (§8). Plus the review-queue and
    entity-embedding cache tables. Guards every ALTER with a PRAGMA check."""
    cols = {r[1] for r in mem.execute("PRAGMA table_info(entities)").fetchall()}
    if "coalesce_state" not in cols:
        # 'provisional' | 'quarantined' | 'clustered' | NULL. Indexed so the
        # working-set filter is a seek, not a full-table JSON scan.
        mem.execute("ALTER TABLE entities ADD COLUMN coalesce_state TEXT")
    if "cluster_id" not in cols:
        mem.execute("ALTER TABLE entities ADD COLUMN cluster_id TEXT")
    if "resolution_run" not in cols:
        mem.execute("ALTER TABLE entities ADD COLUMN resolution_run TEXT")
    mem.execute("CREATE INDEX IF NOT EXISTS idx_entities_coalesce_state ON entities(coalesce_state)")
    mem.execute("CREATE INDEX IF NOT EXISTS idx_entities_cluster ON entities(cluster_id)")

    mem.execute(
        """CREATE TABLE IF NOT EXISTS entity_coalesce_candidates (
            uuid TEXT PRIMARY KEY,
            entity_a TEXT NOT NULL,
            entity_b TEXT NOT NULL,
            name_a TEXT, name_b TEXT,
            cosine REAL, fuzzy INTEGER,
            band TEXT,                 -- 'merge' | 'needs_llm'
            verdict TEXT,              -- 'merge' | 'related_not_same' | 'uncertain' | NULL
            resolution_run TEXT,
            detected_at TEXT,
            reviewed_at TEXT,
            review_action TEXT,        -- 'merge' | 'related' | 'reject' | 'defer'
            metadata TEXT
        )"""
    )
    mem.execute("CREATE INDEX IF NOT EXISTS idx_ecc_reviewed ON entity_coalesce_candidates(reviewed_at)")
    # Entity-name embedding cache (persisted; re-runs only embed new/changed —
    # keyed on name hash). Also reusable for semantic entity search.
    mem.execute(
        """CREATE TABLE IF NOT EXISTS entity_embeddings (
            entity_id TEXT PRIMARY KEY,
            name_hash TEXT NOT NULL,
            embedding BLOB NOT NULL,
            dim INTEGER NOT NULL,
            model TEXT,
            created_at TEXT
        )"""
    )


def _name_hash(name: str) -> str:
    import hashlib
    return hashlib.sha1((name or "").strip().lower().encode("utf-8"),
                        usedforsecurity=False).hexdigest()[:16]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _embed_tier_info(model_seen: Optional[str]) -> dict:
    """Report which embed tier served the run + a §8 perf hint. The embed
    cascade picks tier-1 (in-process Rust, ~5× faster) only when M3_EMBED_GGUF
    is set; otherwise it silently degrades to the HTTP fallback (§8 footgun). We
    don't change that here — just surface it so the operator isn't blind to a
    slow run. model_seen=None means dry-run / nothing embedded."""
    gguf = (os.environ.get("M3_EMBED_GGUF") or "").strip()
    in_process = bool(gguf) and bool(model_seen) and "gguf" in (model_seen or "").lower()
    info: dict = {"in_process": in_process, "model": model_seen}
    if not in_process:
        info["hint"] = ("embedding used the HTTP fallback tier; set M3_EMBED_GGUF "
                        "to a local BGE-M3 GGUF for the ~5x-faster in-process tier (§8).")
    return info


# ── Stage 0: prune -> quarantine (reversible flag, never delete) ─────────────
def _quarantine_noise(mem: sqlite3.Connection, run_id: str, dry_run: bool) -> dict:
    """Tag provisional entities that are clearly not entities (deny-list +
    noise regex). Reversible: sets coalesce_state='quarantined'; never deletes."""
    rows = mem.execute(
        "SELECT id, canonical_name FROM entities "
        "WHERE entity_type = 'unknown' "
        "  AND (coalesce_state IS NULL OR coalesce_state = 'provisional')"
    ).fetchall()
    to_q = [(r[0], r[1]) for r in rows if _is_noise(r[1])]
    if not dry_run and to_q:
        mem.executemany(
            "UPDATE entities SET coalesce_state='quarantined', resolution_run=? WHERE id=?",
            [(run_id, eid) for eid, _ in to_q],
        )
    return {"scanned": len(rows), "quarantined": len(to_q),
            "samples": [n for _, n in to_q[:15]]}


def _prime_embeddings(mem, items, dry_run):
    """Batch-embed (§4 — one cascade call, not per-name HTTP) the given
    [(eid, name)] whose cached embedding is missing/stale, and persist them.
    Returns the count embedded. dry_run: skip embedding entirely (estimate only;
    a dry-run must not make hundreds of embed calls). Returns (count, model)."""
    if dry_run:
        return 0, None
    need_ids, need_names = [], []
    for eid, name in items:
        nh = _name_hash(name)
        row = mem.execute(
            "SELECT name_hash FROM entity_embeddings WHERE entity_id=?", (eid,)
        ).fetchone()
        if not (row and row[0] == nh):
            need_ids.append((eid, name, nh))
            need_names.append(name)
    if not need_names:
        return 0, None
    from embedding_utils import pack

    from .embed import embed_texts
    results = embed_texts(need_names)  # ONE batched cascade call
    n = 0
    model_seen = None
    for (eid, name, nh), (vec, model) in zip(need_ids, results):
        if vec is None:
            continue
        model_seen = model
        mem.execute(
            "INSERT OR REPLACE INTO entity_embeddings"
            "(entity_id, name_hash, embedding, dim, model, created_at) VALUES (?,?,?,?,?,?)",
            (eid, nh, pack(vec), len(vec), model, _now()),
        )
        n += 1
    mem.execute("COMMIT")   # commit the batch -> kill-and-resume safe
    mem.execute("BEGIN")
    return n, model_seen


def _cached_embedding(mem, eid):
    """Read-only: (packed, dim) from the cache, or (None, 0). Used in the
    pairwise loop — never embeds (priming already did)."""
    row = mem.execute(
        "SELECT embedding, dim FROM entity_embeddings WHERE entity_id=?", (eid,)
    ).fetchone()
    return (row[0], row[1]) if row else (None, 0)


def _cluster_size(mem, cid):
    if not cid:
        return 1
    return mem.execute("SELECT count(*) FROM entities WHERE cluster_id=?", (cid,)).fetchone()[0]


def coalesce_detect(*, corpus=None, max_pairs=MAX_PAIRS, dry_run=False, db_path=None):
    """v1 batch pass: quarantine noise + detect coalescing candidates into the
    review queue. Writes ONLY quarantine flags + candidate rows + the embedding
    cache (no merges, no auto-apply). Returns structured counts (§3)."""
    t0 = time.perf_counter()
    run_id = "coalesce-" + _now()
    out: dict = {"run_id": run_id, "dry_run": dry_run}
    try:
        with _memory_db() as mem:
            ensure_schema(mem)
            mem.execute("BEGIN")
            out["prune"] = _quarantine_noise(mem, run_id, dry_run)

            ents = mem.execute(
                "SELECT id, canonical_name, cluster_id FROM entities "
                "WHERE entity_type='unknown' "
                "  AND COALESCE(coalesce_state,'provisional')='provisional' "
                "  AND canonical_name IS NOT NULL"
            ).fetchall()

            blocks: dict = {}
            for eid, name, cid in ents:
                if _is_noise(name):
                    continue
                blocks.setdefault(_block_key(name), []).append((eid, name, cid))
            multi = {k: v for k, v in blocks.items() if k and len(v) > 1}
            out["blocks_total"] = len(blocks)
            out["blocks_multi"] = len(multi)
            out["singletons_skipped"] = sum(1 for v in blocks.values() if len(v) == 1)

            existing: set = set()
            for a, b in mem.execute(
                "SELECT entity_a, entity_b FROM entity_coalesce_candidates WHERE reviewed_at IS NULL"
            ).fetchall():
                existing.add((a, b)); existing.add((b, a))

            try:
                from rapidfuzz import fuzz
                def _fz(a, b):
                    return int(fuzz.token_sort_ratio(a.lower(), b.lower()))
            except ImportError:
                import difflib
                def _fz(a, b):
                    return int(difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() * 100)

            # Batch-prime embeddings for all multi-block members up front (§4 —
            # one cascade call, not per-name HTTP in the loop). dry_run skips
            # embedding (estimate only). Then the loop reads the cache.
            prime_items = []
            for members in multi.values():
                for eid, name, _cid in members[:MAX_BLOCK]:
                    prime_items.append((eid, name))
            out["embedded"], _model_seen = _prime_embeddings(mem, prime_items, dry_run)
            out["embed_tier"] = _embed_tier_info(_model_seen)

            recorded = 0
            for key, members in multi.items():
                if recorded >= max_pairs:
                    break
                if len(members) > MAX_BLOCK:
                    members = members[:MAX_BLOCK]
                for i in range(len(members)):
                    if recorded >= max_pairs:
                        break
                    ai, an, acid = members[i]
                    for j in range(i + 1, len(members)):
                        bi, bn, bcid = members[j]
                        if (ai, bi) in existing:
                            continue
                        fz = _fz(an, bn)
                        cos = 0.0
                        if fz < FUZZY_HIGH:
                            if dry_run:
                                continue  # no embeddings primed in dry-run
                            ea, da = _cached_embedding(mem, ai)
                            eb, db_ = _cached_embedding(mem, bi)
                            if ea and eb and da == db_:
                                cos = _cosine_packed(ea, eb, da)
                            if cos < FLAG_COSINE:
                                continue
                        else:
                            cos = fz / 100.0
                        over = (_cluster_size(mem, acid) + _cluster_size(mem, bcid)) > MAX_CLUSTER
                        # Collision guards: leading-`_` (private-vs-public) and
                        # trailing-number (distinct config/version) pairs score
                        # 0.95+/95+ but are usually DIFFERENT entities — never
                        # auto-merge, route to review. (Both validated, live v1.)
                        if (cos >= AUTO_MERGE_COSINE and fz >= FUZZY_HIGH
                                and not over
                                and not _underscore_collision(an, bn)
                                and not _numeric_suffix_collision(an, bn)):
                            band = "merge"
                        else:
                            band = "needs_llm"
                        if not dry_run:
                            mem.execute(
                                "INSERT INTO entity_coalesce_candidates"
                                "(uuid, entity_a, entity_b, name_a, name_b, cosine, fuzzy,"
                                " band, resolution_run, detected_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                (str(_uuid.uuid4()), ai, bi, an, bn, cos, fz, band, run_id, _now()),
                            )
                        existing.add((ai, bi)); existing.add((bi, ai))
                        recorded += 1
                        if recorded >= max_pairs:
                            break
            out["candidates_recorded"] = recorded
            mem.execute("COMMIT" if not dry_run else "ROLLBACK")
    except Exception as e:
        logger.warning("coalesce_detect failed: %s", e)
        out["error"] = f"{type(e).__name__}: {e}"
    out["duration_ms"] = int((time.perf_counter() - t0) * 1000)
    return out


def list_coalesce_candidates(*, reviewed=False, limit=100, min_cosine=None):
    """List coalescing candidate pairs (structured rows, §3). reviewed: None=both,
    False=unreviewed, True=reviewed."""
    sql = ["SELECT uuid, entity_a, entity_b, name_a, name_b, cosine, fuzzy, band,"
           " verdict, detected_at, reviewed_at, review_action "
           "FROM entity_coalesce_candidates WHERE 1=1"]
    params: list = []
    if reviewed is False:
        sql.append("AND reviewed_at IS NULL")
    elif reviewed is True:
        sql.append("AND reviewed_at IS NOT NULL")
    if min_cosine is not None:
        sql.append("AND cosine >= ?"); params.append(min_cosine)
    sql.append("ORDER BY cosine DESC, fuzzy DESC LIMIT ?"); params.append(limit)
    with _memory_db() as mem:
        mem.row_factory = sqlite3.Row
        rows = mem.execute(" ".join(sql), params).fetchall()
    return [
        {"uuid": r["uuid"], "band": r["band"], "verdict": r["verdict"],
         "cosine": round(r["cosine"] or 0.0, 4), "fuzzy": r["fuzzy"],
         "detected_at": r["detected_at"], "reviewed_at": r["reviewed_at"],
         "review_action": r["review_action"],
         "entity_a": {"id": r["entity_a"], "name": r["name_a"]},
         "entity_b": {"id": r["entity_b"], "name": r["name_b"]}}
        for r in rows
    ]


def review_coalesce_candidates(reviews, *, note=""):
    """BULK review (§3 — take a LIST). Each item {uuid, action}; action in
    merge|related|reject|defer. v1 RECORDS the decision only — no overlay
    applied (that is v2). Empty list -> {updated:0}."""
    valid = {"merge", "related", "reject", "defer"}
    if not isinstance(reviews, list):
        raise ValueError("reviews must be a list of {uuid, action}")
    now = _now()
    updated, errors = 0, []
    with _memory_db() as mem:
        mem.execute("BEGIN")
        for item in reviews:
            if not isinstance(item, dict) or "uuid" not in item or "action" not in item:
                errors.append({"item": item, "error": "needs {uuid, action}"}); continue
            if item["action"] not in valid:
                errors.append({"uuid": item.get("uuid"), "error": f"action must be {sorted(valid)}"}); continue
            cur = mem.execute(
                "UPDATE entity_coalesce_candidates SET reviewed_at=?, review_action=?, "
                "metadata=json_set(COALESCE(metadata,'{}'),'$.note',?) WHERE uuid=?",
                (now, item["action"], note, item["uuid"]),
            )
            if cur.rowcount:
                updated += 1
            else:
                errors.append({"uuid": item["uuid"], "error": "not found"})
        mem.execute("COMMIT")
    return {"updated": updated, "errors": errors, "reviewed_at": now}


# ── v2: apply the reversible coalescence OVERLAY ─────────────────────────────
# A "merge" is NOT destructive — members stay intact. We set a shared cluster_id
# + write a `same_as` edge (member -> representative). Canonical view = read-time
# projection (follow same_as). Reversal = drop the edge + clear cluster_id. No
# entity is deleted, no fact_entity_refs migrated. (Lesson #1: reversible by
# construction.)

def _pick_representative(mem, eid_a, eid_b):
    """Deterministic canonical pick (NOT the LLM's name — lesson #3): higher
    files.db ref-degree wins; tie -> longer (more complete) name; tie -> lower id."""
    def degree(eid):
        # ref count in files.db; falls back to 0 if unavailable (read-only probe)
        try:
            from .db import _db as _files_db
            with _files_db() as f:
                return f.execute(
                    "SELECT count(*) FROM fact_entity_refs WHERE entity_uuid=?", (eid,)
                ).fetchone()[0]
        except Exception:
            return 0
    a = mem.execute("SELECT id, canonical_name, entity_type FROM entities WHERE id=?", (eid_a,)).fetchone()
    b = mem.execute("SELECT id, canonical_name, entity_type FROM entities WHERE id=?", (eid_b,)).fetchone()
    da, db_ = degree(eid_a), degree(eid_b)
    if da != db_:
        rep, mem_ent = (a, b) if da > db_ else (b, a)
    elif len((a[1] or "")) != len((b[1] or "")):
        rep, mem_ent = (a, b) if len(a[1] or "") >= len(b[1] or "") else (b, a)
    else:
        rep, mem_ent = (a, b) if a[0] <= b[0] else (b, a)
    return rep, mem_ent


def apply_coalescence(*, candidate_uuids=None, include_auto_merge=False,
                      dry_run=False, db_path=None, confirm=False):
    """Apply the reversible same_as/cluster overlay for coalescing candidates.

    Sources (union): explicit candidate_uuids that are reviewed merge OR (if
    include_auto_merge) the 'merge'-band candidates (clean after the underscore
    guard). For each pair: pick a deterministic representative, give the member
    the rep's cluster_id, mark it coalesce_state='clustered', and write a
    `same_as` (member -> rep) edge. Members are NEVER deleted; reversal via
    unapply_cluster(). Returns structured counts. dry_run reports without writing.

    MUTATION GUARD: this WRITES to the core entity graph. To avoid silently
    mutating the production DB, a real (non-dry-run) apply must EITHER target an
    explicit `db_path` OR pass `confirm=True` to acknowledge it will write to the
    resolved core DB. dry_run is always allowed (it writes nothing)."""
    if not dry_run and db_path is None and not confirm:
        return {"applied": 0, "skipped": 0, "clusters_touched": [], "dry_run": False,
                "error": "refused: a real apply must pass db_path=<target> or "
                          "confirm=True (it writes to the core entity graph). "
                          "Use dry_run=True to preview."}
    out: dict = {"applied": 0, "skipped": 0, "clusters_touched": set(), "dry_run": dry_run}
    with _memory_db(db_path) as mem:
        ensure_schema(mem)
        mem.execute("BEGIN")
        # collect target candidates
        sel = ("SELECT uuid, entity_a, entity_b, band, review_action "
               "FROM entity_coalesce_candidates WHERE ")
        clauses, params = [], []
        if candidate_uuids:
            qs = ",".join("?" * len(candidate_uuids))
            clauses.append(f"(uuid IN ({qs}) AND review_action='merge')")
            params += list(candidate_uuids)
        if include_auto_merge:
            clauses.append("(band='merge' AND reviewed_at IS NULL)")
        if not clauses:
            mem.execute("ROLLBACK")
            return {**out, "clusters_touched": [], "note": "nothing selected"}
        rows = mem.execute(sel + " OR ".join(clauses), params).fetchall()
        for cuuid, ea, eb, band, action in rows:
            rep, m = _pick_representative(mem, ea, eb)
            if rep is None or m is None:
                out["skipped"] += 1
                continue
            cid = mem.execute(
                "SELECT cluster_id FROM entities WHERE id=?", (rep[0],)
            ).fetchone()[0] or ("cluster-" + rep[0][:12])
            if not dry_run:
                # representative gets/keeps the cluster_id
                mem.execute("UPDATE entities SET cluster_id=? WHERE id=?", (cid, rep[0]))
                # member joins the cluster (kept intact, flagged clustered)
                mem.execute(
                    "UPDATE entities SET cluster_id=?, coalesce_state='clustered' WHERE id=?",
                    (cid, m[0]),
                )
                # reversible same_as edge: member -> representative
                mem.execute(
                    "INSERT INTO entity_relationships(from_entity, to_entity, predicate, "
                    "confidence, valid_from, created_at) VALUES (?,?,?,?,?,?)",
                    (m[0], rep[0], "same_as", 0.95, _now(), _now()),
                )
                # add the member's name as an alias on the representative
                mem.execute(
                    "UPDATE entities SET attributes_json=json_set("
                    "COALESCE(attributes_json,'{}'),'$.aliases',"
                    "json_insert(COALESCE(json_extract(attributes_json,'$.aliases'),'[]'),"
                    "'$[#]', ?)) WHERE id=?",
                    (m[1], rep[0]),
                )
                mem.execute(
                    "UPDATE entity_coalesce_candidates SET reviewed_at=COALESCE(reviewed_at,?), "
                    "review_action='merge', verdict='merge' WHERE uuid=?",
                    (_now(), cuuid),
                )
            out["applied"] += 1
            out["clusters_touched"].add(cid)
        mem.execute("COMMIT" if not dry_run else "ROLLBACK")
    out["clusters_touched"] = sorted(out["clusters_touched"])
    return out


def unapply_cluster(cluster_id: str, *, db_path=None) -> dict:
    """Reverse a coalescence (lesson #1 — trivial undo): drop the same_as edges
    into the cluster representative and clear cluster_id/coalesce_state on its
    members. Members were never deleted, so this fully restores the prior state.

    `db_path` targets an explicit DB (else the resolved core DB). Unapply is
    inherently safe (it only REMOVES overlay edges/flags — never touches member
    data), so no confirm guard is needed."""
    with _memory_db(db_path) as mem:
        mem.execute("BEGIN")
        members = [r[0] for r in mem.execute(
            "SELECT id FROM entities WHERE cluster_id=?", (cluster_id,)).fetchall()]
        if members:
            qs = ",".join("?" * len(members))
            mem.execute(f"DELETE FROM entity_relationships WHERE predicate='same_as' "
                        f"AND from_entity IN ({qs})", members)
            mem.execute(f"UPDATE entities SET cluster_id=NULL, "
                        f"coalesce_state=CASE WHEN entity_type='unknown' THEN 'provisional' ELSE coalesce_state END "
                        f"WHERE id IN ({qs})", members)
        mem.execute("COMMIT")
    return {"cluster_id": cluster_id, "reverted_members": len(members)}
