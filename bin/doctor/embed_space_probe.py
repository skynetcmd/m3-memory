"""Mixed embed-space probe — are stored vectors all cosine-comparable?

Cosine similarity is only meaningful between vectors from the SAME embedding
model. If a store accumulates rows from two different models, search silently
returns garbage rankings for the minority rows: nothing errors, nothing logs,
and the operator sees only "search got worse". docs/EMBED_INPUT_RECIPE.md
states the hard rule — never mix embeddings from different models or quants in
the same database.

This is a REAL hazard, not a theoretical one. Until 2026-07-23 the README's
copy-paste install prompts told new users to run Ollama with
``qwen3-embedding:0.6b`` "for best retrieval", while the shipped default is
in-process BGE-M3. Anyone who followed that prompt and later fell back to the
default has a store with two incompatible vector spaces in it.

The probe groups tags into FAMILIES rather than comparing them literally,
because one model legitimately carries several tags: the in-process GGUF, the
llama-server HTTP path and the CPU fallback all tag ``bge-m3-GGUF-Q4_K_M.gguf``
while LM Studio tags the same model ``text-embedding-bge-m3``. Those are
cosine-comparable (parity-verified ~0.996) and must NOT be reported as a mix —
a false positive here tells a healthy operator their store is corrupt.

Report-only: never fails the doctor run (returns 0). A mixed store is a
warning the operator must act on deliberately (re-embedding is a one-time cost
they pay explicitly), not something a health check should decide for them.
"""
from __future__ import annotations

import logging
import os
import re
import sys

logger = logging.getLogger("memory.doctor.embed_space_probe")

# Rows below this share of the store are the "minority" space — the ones whose
# search results are silently wrong. Used only to phrase the remediation hint.
_MINORITY_SHARE = 0.5

# Tag → family. A family is a set of tags whose vectors ARE cosine-comparable.
# Order matters: first match wins, so put specific patterns before generic ones.
# Source of truth for the bge-m3 aliases: docs/EMBED_INPUT_RECIPE.md.
_FAMILY_PATTERNS = (
    (re.compile(r"qwen.?3.*embed", re.I), "qwen3-embedding"),
    (re.compile(r"bge.?m3", re.I), "bge-m3"),
    (re.compile(r"nomic.*embed", re.I), "nomic-embed"),
    (re.compile(r"jina.*embed", re.I), "jina-embed"),
    (re.compile(r"^fake-model$", re.I), "test-fixture"),
)


def _family(tag: str) -> str:
    """Collapse an embed_model tag to its cosine-comparable family.

    Unknown tags map to themselves, so a model we have never seen is treated as
    its own space (conservative — better to flag an unknown mix than to assume
    compatibility we cannot verify).
    """
    t = (tag or "").strip()
    if not t:
        return "<untagged>"
    for pat, fam in _FAMILY_PATTERNS:
        if pat.search(t):
            return fam
    return t


def run(brief: bool = False) -> int:
    """Report whether the store mixes incompatible embedding spaces.

    Always returns 0 (report-only). Never raises: a probe must not crash the
    doctor run.
    """
    bin_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if bin_dir not in sys.path:
        sys.path.insert(0, bin_dir)

    try:
        from m3_core.paths import resolve_engine_file
        from memory.backends import active_backend
    except Exception as e:  # noqa: BLE001 — probe must never crash the doctor
        if brief:
            print("embed-space: unknown (storage seam not loadable)")
        else:
            print(f"  status   : could not load storage seam: {type(e).__name__}: {e}")
        return 0

    # Group by (vector_kind, embed_model, dim). vector_kind partitions vectors
    # that are SUPPOSED to live in separate spaces (e.g. a fallback space), so a
    # mix is only a problem WITHIN one kind.
    rows: "list[tuple[str, str, int, int]]" = []
    try:
        db_path = resolve_engine_file("agent_memory.db")
        with active_backend().open_readonly(db_path) as conn:
            cur = conn.execute(
                "SELECT COALESCE(vector_kind, 'default'), COALESCE(embed_model, ''), "
                "       COALESCE(dim, 0), COUNT(*) "
                "FROM memory_embeddings GROUP BY 1, 2, 3"
            )
            rows = [(str(r[0]), str(r[1]), int(r[2]), int(r[3])) for r in cur.fetchall()]
    except Exception as e:  # noqa: BLE001 — missing table on a fresh store is fine
        if brief:
            print("embed-space: unknown (no embeddings table yet)")
        else:
            print(f"  status   : could not read memory_embeddings: "
                  f"{type(e).__name__}: {e}")
        return 0

    if not rows:
        if brief:
            print("embed-space: ok (no embeddings yet)")
        else:
            print()
            print("=== embed space ===")
            print("  no embedded rows yet — nothing to compare.")
        return 0

    # Fold to families per vector_kind, and track dim conflicts separately: a
    # dim mismatch is a HARDER error (cosine cannot even be computed) than a
    # same-dim family mix (computes fine, returns nonsense).
    by_kind: "dict[str, dict[str, int]]" = {}
    dims_by_kind: "dict[str, set[int]]" = {}
    tags_by_family: "dict[tuple[str, str], set[str]]" = {}
    for kind, tag, dim, n in rows:
        fam = _family(tag)
        by_kind.setdefault(kind, {})
        by_kind[kind][fam] = by_kind[kind].get(fam, 0) + n
        dims_by_kind.setdefault(kind, set()).add(dim)
        tags_by_family.setdefault((kind, fam), set()).add(tag or "<untagged>")

    mixed = {k: fams for k, fams in by_kind.items() if len(fams) > 1}
    dim_split = {k: d for k, d in dims_by_kind.items() if len(d) > 1}

    if brief:
        if mixed:
            worst = max(mixed.items(), key=lambda kv: len(kv[1]))
            print(f"embed-space: MIXED — {len(worst[1])} incompatible model families "
                  f"in '{worst[0]}' (search rankings unreliable)")
        elif dim_split:
            print("embed-space: MIXED dimensions — cosine cannot be computed")
        else:
            fam = next(iter(next(iter(by_kind.values()))))
            print(f"embed-space: ok (single space: {fam})")
        return 0

    print()
    print("=== embed space ===")

    if not mixed and not dim_split:
        for kind, fams in sorted(by_kind.items()):
            fam, n = next(iter(fams.items()))
            tags = sorted(tags_by_family[(kind, fam)])
            alias = f" (tags: {', '.join(tags)})" if len(tags) > 1 else ""
            print(f"  {kind:10} : {fam} — {n:,} vectors{alias}")
        print("  status   : OK — all vectors are cosine-comparable.")
        return 0

    for kind, fams in sorted(mixed.items()):
        total = sum(fams.values())
        print(f"  MIXED SPACE in vector_kind '{kind}' — {len(fams)} incompatible "
              f"model families share one index:")
        for fam, n in sorted(fams.items(), key=lambda kv: -kv[1]):
            share = n / total if total else 0.0
            tags = sorted(tags_by_family[(kind, fam)])
            alias = f"  [{', '.join(tags)}]" if len(tags) > 1 else ""
            flag = "  <-- minority: these rows rank wrongly" if share < _MINORITY_SHARE else ""
            print(f"    - {fam:22} {n:>8,} ({share:5.1%}){alias}{flag}")
        print("    Why it matters: cosine similarity across different embedding "
              "models is meaningless, so the minority rows are effectively "
              "unsearchable — silently, with no error.")
        print("    Fix: re-embed the whole store with one model "
              "(see docs/EMBED_INPUT_RECIPE.md). This is a deliberate one-time "
              "cost, so m3 will not do it automatically.")

    for kind, dims in sorted(dim_split.items()):
        print(f"  MIXED DIMENSIONS in vector_kind '{kind}': "
              f"{', '.join(str(d) for d in sorted(dims))} — vectors of different "
              f"lengths cannot be compared at all.")

    return 0
