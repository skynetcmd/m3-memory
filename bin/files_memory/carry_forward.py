"""Leaf-level carry-forward on file_node supersession.

When a re-ingest changes a file's content_sha256, most leaves typically
have NOT changed (e.g. one typo fix in a 200-page PDF re-embeds 199
pages for no reason). Carry-forward detects identical leaves between
the prior and new file_node versions and:

  1. Copies the embedding rows instead of re-running the embedder.
  2. Sets evolved_from on the new leaf so the graph traversal can find
     the prior version.
  3. Preserves the old leaf's extraction_status — if it was 'ok' with
     facts attached, the new leaf inherits the same facts (cloned into
     the new ingestion_run for provenance, see _carry_facts).

When content differs but the division aligns (same division_id), the
new leaf is marked 'evolved' with a material_change flag set by an
optional LLM judgment pass.

Public API:
    compute_leaf_diffs(conn, prior_file_node, new_leaves) -> list[LeafDiff]
    apply_carry_forward(conn, diff, new_leaf_uuid) -> bool
    judge_material_change(old_text, new_text) -> bool
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("files_memory.carry_forward")


@dataclass
class LeafDiff:
    """Per-leaf classification for the new file_node version.

    kind:
      'carry'   — identical content; copy embedding, reuse facts.
      'evolve'  — same division_id, different content; embed normally,
                  set evolved_from, optionally re-extract.
      'new'     — no matching division in prior version; normal ingest.

    For 'evolve' kind, `material_change` is filled in later by the
    LLM judge (or set to True conservatively when no LLM is configured).
    """
    new_index: int                 # index into the new_leaves list
    kind: str
    prior_uuid: Optional[str] = None
    prior_text: Optional[str] = None
    material_change: Optional[bool] = None


@dataclass
class CarryForwardStats:
    """Counters surfaced to the ingest caller."""
    carry: int = 0
    evolve: int = 0
    new: int = 0
    embeds_avoided: int = 0
    facts_carried: int = 0


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_leaf_diffs(
    conn: sqlite3.Connection,
    prior_file_node_uuid: str,
    new_leaves: list,  # list[Leaf] from chunkers
) -> list[LeafDiff]:
    """Build the per-leaf diff list against the prior version's leaves.

    Two indexes from prior leaves:
      - text_sha256 → leaf_uuid (carry candidates)
      - (division_type, division_id) → leaf_uuid (evolve candidates)

    Each new leaf is classified greedily: carry wins over evolve, evolve
    wins over new. We don't try to match many-to-many (one new leaf to
    multiple priors); first match wins.
    """
    by_hash: dict[str, str] = {}
    by_division: dict[tuple[str, str], tuple[str, str]] = {}  # (type,id) -> (uuid, text)

    rows = conn.execute(
        "SELECT uuid, text, text_sha256, division_type, division_id "
        "FROM leaves WHERE file_node = ?",
        (prior_file_node_uuid,),
    ).fetchall()
    for r in rows:
        h = r["text_sha256"] if "text_sha256" in r.keys() else r[2]
        u = r["uuid"] if "uuid" in r.keys() else r[0]
        t = r["text"] if "text" in r.keys() else r[1]
        dt = r["division_type"] if "division_type" in r.keys() else r[3]
        di = r["division_id"] if "division_id" in r.keys() else r[4]
        by_hash.setdefault(h, u)
        by_division.setdefault((dt, di), (u, t))

    diffs: list[LeafDiff] = []
    used_prior: set[str] = set()  # avoid double-mapping same prior leaf
    for i, leaf in enumerate(new_leaves):
        h = _text_sha256(leaf.text)
        carry_uuid = by_hash.get(h)
        if carry_uuid and carry_uuid not in used_prior:
            used_prior.add(carry_uuid)
            diffs.append(LeafDiff(
                new_index=i, kind="carry", prior_uuid=carry_uuid,
            ))
            continue

        # Try same-division match for 'evolve'
        evo = by_division.get((leaf.division_type, leaf.division_id))
        if evo and evo[0] not in used_prior:
            used_prior.add(evo[0])
            diffs.append(LeafDiff(
                new_index=i, kind="evolve", prior_uuid=evo[0],
                prior_text=evo[1],
            ))
            continue

        diffs.append(LeafDiff(new_index=i, kind="new"))

    return diffs


def apply_carry_forward(
    conn: sqlite3.Connection,
    diff: LeafDiff,
    new_leaf_uuid: str,
    *,
    carry_facts: bool = True,
    new_ingestion_run_uuid: Optional[str] = None,
) -> tuple[bool, int]:
    """Copy embedding + (optionally) facts from prior leaf to new leaf.

    Returns (embedding_copied, facts_copied).

    The new leaf row must already exist (with evolved_from set). This
    function copies the prior leaf's data into the new leaf row's
    associated tables.
    """
    if diff.kind != "carry" or not diff.prior_uuid:
        return (False, 0)

    # Copy text + summary embeddings (whichever kinds exist).
    cur = conn.execute(
        "INSERT OR REPLACE INTO leaf_embeddings(leaf_uuid, kind, embedding, embed_model, dim) "
        "SELECT ?, kind, embedding, embed_model, dim "
        "FROM leaf_embeddings WHERE leaf_uuid = ?",
        (new_leaf_uuid, diff.prior_uuid),
    )
    embedded = cur.rowcount > 0
    if embedded:
        conn.execute(
            "UPDATE leaves SET embedded = 1 WHERE uuid = ?", (new_leaf_uuid,),
        )

    facts_copied = 0
    if carry_facts:
        # Copy facts from the old leaf to the new leaf. Re-uuid each
        # fact so it has its own identity; keep the same statement,
        # source_span, confidence. extraction_run points at the NEW
        # ingestion run so the audit trail says "carried, not extracted".
        prior_facts = conn.execute(
            "SELECT uuid, statement, source_span_start, source_span_end, confidence "
            "FROM facts WHERE leaf = ?",
            (diff.prior_uuid,),
        ).fetchall()
        import uuid as _uuid
        for pf in prior_facts:
            new_fact_uuid = str(_uuid.uuid4())
            new_file_node = conn.execute(
                "SELECT file_node FROM leaves WHERE uuid = ?", (new_leaf_uuid,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO facts(uuid, leaf, file_node, statement, "
                "source_span_start, source_span_end, confidence, extraction_run) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_fact_uuid, new_leaf_uuid, new_file_node,
                    pf["statement"], pf["source_span_start"], pf["source_span_end"],
                    pf["confidence"],
                    new_ingestion_run_uuid or pf["uuid"],  # safe fallback
                ),
            )
            # Carry the fact's embedding too.
            conn.execute(
                "INSERT OR REPLACE INTO fact_embeddings(fact_uuid, embedding, embed_model, dim) "
                "SELECT ?, embedding, embed_model, dim FROM fact_embeddings "
                "WHERE fact_uuid = ?",
                (new_fact_uuid, pf["uuid"]),
            )
            # Carry entity refs.
            conn.execute(
                "INSERT OR IGNORE INTO fact_entity_refs(fact, entity_uuid, confidence) "
                "SELECT ?, entity_uuid, confidence FROM fact_entity_refs "
                "WHERE fact = ?",
                (new_fact_uuid, pf["uuid"]),
            )
            facts_copied += 1
        if prior_facts:
            # Mark the leaf as already-extracted (no work needed for queue/inline).
            conn.execute(
                "UPDATE leaves SET extraction_status = 'ok' WHERE uuid = ?",
                (new_leaf_uuid,),
            )

    return (embedded, facts_copied)


# ──────────────────────────────────────────────────────────────────────────────
# Material-change judgment (LLM-optional)
# ──────────────────────────────────────────────────────────────────────────────
_JUDGE_PROMPT = """\
You compare two versions of a section from a document. Decide whether the
substance (facts, claims, numbers, named entities) has materially changed,
or whether the differences are only cosmetic (whitespace, formatting,
typo fixes, reordering of equivalent phrasing).

Output strict JSON only, no prose:
{"material_change": true|false, "reason": "<one short sentence>"}

Examples of NOT material:
- Typo fixes ("recieve" -> "receive")
- Whitespace / line break changes
- Re-ordering bullet points that say the same thing
- Capitalization changes

Examples of material:
- A different number, date, name, or measurement
- An added or removed claim
- A reversed conclusion
- A new or dropped reference to an entity"""


def judge_material_change(old_text: str, new_text: str) -> tuple[Optional[bool], Optional[str]]:
    """Use the configured LLM to judge whether two leaves' content differs
    materially. Returns (flag, reason).

    Falls back to (None, None) when no LLM is configured — callers should
    treat None as "unknown, conservatively assume material" when storing
    the flag.
    """
    from .extract import _llm_endpoint, _llm_model  # reuse extract's client config

    endpoint = _llm_endpoint()
    if not endpoint:
        return (None, None)

    if old_text.strip() == new_text.strip():
        return (False, "identical after whitespace normalization")

    import httpx

    user_content = f"OLD VERSION:\n{old_text[:3000]}\n\nNEW VERSION:\n{new_text[:3000]}"
    url = endpoint.rstrip("/") + "/v1/chat/completions"
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                url,
                json={
                    "model": _llm_model(),
                    "messages": [
                        {"role": "system", "content": _JUDGE_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 128,
                    "response_format": {"type": "json_object"},
                },
            )
            resp.raise_for_status()
            data = resp.json()
            raw = (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        logger.debug("material_change judge failed: %s", e)
        return (None, None)

    # Parse the JSON response defensively.
    if raw.startswith("```"):
        import re
        raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
        raw = re.sub(r"\n```\s*$", "", raw)
    first = raw.find("{")
    last = raw.rfind("}")
    if first < 0 or last <= first:
        return (None, None)
    try:
        obj = json.loads(raw[first:last + 1])
    except (ValueError, json.JSONDecodeError):
        return (None, None)
    flag = obj.get("material_change")
    reason = (obj.get("reason") or "").strip() or None
    if isinstance(flag, bool):
        return (flag, reason)
    return (None, reason)
