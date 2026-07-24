"""Compile-at-ingest: turn a topic cluster into a durable `synthesis` memory row.

Where `synth.py` produces a throwaway prose *lede* cached to disk, this module
persists the compiled prose as a first-class memory (`type="synthesis"`) so it
gains supersession, contradiction edges, aging, revocation, and graph traversal —
the machinery a *wrong* synthesis needs even more than a right one.

Design (see the plan, ~/.m3-private/plans/WIKI_SYNTHESIS_MEMORY_TYPE.md):

  * **confidence = MIN of member confidences.** A synthesis is only as
    trustworthy as its weakest source; mean/max would let one shaky member ride
    in under a confident average. NULLs are excluded; all-NULL falls back to a
    documented constant.
  * **authority defaults to "provisional".** Nothing renders as canonical body
    prose until promoted (that gate is commit 3).
  * **compiler provenance** rides metadata_json: prompt_version, model,
    cluster_hash (the no-op gate), member_ids (the source manifest), and the id
    of the stored prompt text — so a superseded synthesis can be traced to the
    exact prompt that produced it, even after the prompt is edited.
  * **Write rule (idempotent):** cluster hash matches head → skip (no model
    call, no row). Prose byte-identical to head → skip. Prose differs →
    supersede the head. NEVER update in place — that destroys the audit trail
    and the target a `contradicts` edge needs.

The prose generator is INJECTED (a `ProseCompiler`), exactly like `synth.py`'s
synthesizer, so the whole pipeline — hashing, min-confidence, prompt dedupe,
supersede decision, idempotence — is deterministically testable with a stub and
no model is called on the drift-tested path.

Backend rules (§1): this module writes through `memory_write_impl` and never
emits SQL. WAL checkpointing goes through `backend.maintenance_checkpoint()`;
metadata reads go through `dialect.json_column_to_dict()`. No `if backend ==`.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional, Protocol

from .cluster import Cluster
from .synth import _cluster_hash

# All-NULL fallback for confidence. Compiled prose is DERIVED, never primary, so
# even the fallback stays below 1.0. Documented constant per the plan (§5: a
# number you can defend — this one says "we have no member signal, treat it as
# middling-low rather than inventing confidence").
_NO_MEMBER_CONFIDENCE = 0.5

_PROMPT_TYPE = "reference"  # stored prompt text rides an existing CORE_TYPE...
# ...but is kept OUT of the rendered vault via this marker (checked by the
# renderer / selector); see is_compiler_prompt().
_PROMPT_MARKER = "m3_compiler_prompt"


class ProseCompiler(Protocol):
    """Produces the compiled prose for one cluster. Injected so the pipeline is
    testable with a deterministic stub (mirrors wiki.synth.Synthesizer)."""

    #: A stable identifier for the prompt text this compiler uses, so the writer
    #: can persist + dedupe it. Bump when the prompt changes.
    prompt_version: str
    #: The model identifier (or "" to let the server pick). Recorded in provenance.
    model: str

    def prompt_text(self) -> str:
        """The full prompt text, stored as a memory for audit."""
        ...

    def compile(self, cluster: Cluster) -> Optional[str]:
        """Return compiled markdown prose for the cluster, or None on failure
        (fail-open: a compiler error must never abort the batch)."""
        ...


@dataclass
class CompileResult:
    """Per-cluster outcome, for reporting + the idempotence metric."""
    cluster_key: str
    action: str                 # "skipped-hash" | "skipped-identical" |
                                # "created" | "superseded" | "compile-failed"
    synthesis_id: Optional[str] = None
    superseded_id: Optional[str] = None


@dataclass
class CompileStats:
    created: int = 0
    superseded: int = 0
    skipped_hash: int = 0
    skipped_identical: int = 0
    failed: int = 0
    prompts_written: int = 0
    prompts_reused: int = 0
    results: list[CompileResult] = field(default_factory=list)

    def record(self, r: CompileResult) -> None:
        self.results.append(r)
        {
            "created": lambda: setattr(self, "created", self.created + 1),
            "superseded": lambda: setattr(self, "superseded", self.superseded + 1),
            "skipped-hash": lambda: setattr(self, "skipped_hash", self.skipped_hash + 1),
            "skipped-identical": lambda: setattr(
                self, "skipped_identical", self.skipped_identical + 1),
            "compile-failed": lambda: setattr(self, "failed", self.failed + 1),
        }[r.action]()

    def summary(self) -> str:
        return (f"compile: {self.created} created, {self.superseded} superseded, "
                f"{self.skipped_hash + self.skipped_identical} skipped "
                f"({self.skipped_hash} unchanged, {self.skipped_identical} identical), "
                f"{self.failed} failed; prompts {self.prompts_written} written / "
                f"{self.prompts_reused} reused")


# ── pure helpers (deterministic, unit-tested without a model) ─────────────────

def min_member_confidence(cluster: Cluster) -> float:
    """MIN of member confidences, NULLs excluded; all-NULL → documented constant.

    A synthesis is only as trustworthy as its weakest source. Never returns 1.0
    from members alone here — callers may still cap, but the rule is a floor, not
    a promotion.
    """
    vals = [m.confidence for m in cluster.members if m.confidence is not None]
    if not vals:
        return _NO_MEMBER_CONFIDENCE
    return min(vals)


def prose_hash(prose: str) -> str:
    """Stable content hash of compiled prose, for the byte-identical skip check."""
    return hashlib.sha256((prose or "").encode("utf-8")).hexdigest()


def prompt_content_hash(prompt_text: str) -> str:
    """Dedupe key for the stored prompt: same text → one row, never a second."""
    return hashlib.sha256((prompt_text or "").encode("utf-8")).hexdigest()[:16]


def build_metadata(
    cluster: Cluster,
    compiler: ProseCompiler,
    prompt_memory_id: str,
    *,
    synthesis_kind: str = "compiled",
    authority: str = "provisional",
) -> dict:
    """The synthesis row's metadata_json, as a dict (caller json-dumps it).

    compiler.member_ids is the source manifest — the exact input set, which the
    citation-drift check (commit 4b) reads. cluster_hash is synth.py's no-op
    gate, recorded so a later run can tell "inputs unchanged" without recompute.
    """
    members = cluster.members
    return {
        "synthesis_kind": synthesis_kind,
        "authority": authority,
        "compiler": {
            "prompt_version": compiler.prompt_version,
            "model": compiler.model,
            "cluster_hash": _cluster_hash(cluster, compiler.model),
            "prompt_memory_id": prompt_memory_id,
            "member_ids": [m.id for m in members],
            "member_count": len(members),
        },
        "review": {"state": "pending", "risk": "high"},
        "verdict": None,
    }


def is_compiler_prompt(metadata: dict) -> bool:
    """True if a memory row is a stored compiler prompt (kept out of the vault)."""
    return bool(metadata.get(_PROMPT_MARKER))


# ── async writer (persists through memory_write_impl / memory_supersede_impl) ──
#
# These are the ONLY non-pure functions here. The persistence primitives already
# give us everything the plan needs: memory_write_impl embeds by default and runs
# content-safety at the write boundary (§6); memory_supersede_impl is a
# deterministic, non-destructive supersede that retains the old row with valid_to
# closed and writes the supersedes edge — the "never update in place" rule, built.


async def _ensure_prompt_row(
    compiler: ProseCompiler, stats: CompileStats, *, scope: str, user_id: str,
    _write=None, _search=None,
) -> str:
    """Persist the compiler's prompt text ONCE (deduped by content hash), return
    its memory id. A re-run with an unchanged prompt must not create a 2nd row —
    same §4 discipline as the cluster-hash gate.

    Dedupe is by a stable title carrying the content hash, looked up before write.
    """
    if _write is None:
        from memory.write import memory_write_impl as _write
    text = compiler.prompt_text()
    digest = prompt_content_hash(text)
    title = f"[compiler-prompt] v{compiler.prompt_version} {digest}"

    # Look for an existing prompt row with this exact title (⇒ same content).
    existing = await _find_prompt_by_title(title, _search=_search)
    if existing:
        stats.prompts_reused += 1
        return existing

    meta = {_PROMPT_MARKER: True, "prompt_version": compiler.prompt_version,
            "prompt_hash": digest, "model": compiler.model}
    new_id = await _write(
        type=_PROMPT_TYPE, content=text, title=title,
        metadata=json.dumps(meta),
        # Below any wiki importance threshold + marked, so it never renders as a
        # topic. embed=False: a prompt is not a retrieval target.
        importance=0.0, embed=False, scope=scope or "agent", user_id=user_id,
        source="wiki-compile",
    )
    stats.prompts_written += 1
    return _extract_id(new_id)


async def _find_prompt_by_title(title: str, *, _search=None) -> Optional[str]:
    """Return the id of an existing compiler-prompt row with this exact title,
    or None. Kept narrow: an exact-title equality, not a semantic search."""
    if _search is None:
        try:
            from memory.search import memory_search_impl as _search
        except Exception:
            return None
    try:
        hits = await _search(query=title, k=5, search_mode="keyword")
    except Exception:
        return None
    for h in (hits or []):
        if isinstance(h, dict) and h.get("title") == title:
            return h.get("id") or h.get("memory_id")
    return None


async def compile_cluster(
    cluster: Cluster,
    compiler: ProseCompiler,
    head: "Optional[ExistingSynthesis]",
    prompt_memory_id: str,
    stats: CompileStats,
    *,
    scope: str = "agent",
    user_id: str = "",
    _write=None,
    _supersede=None,
) -> CompileResult:
    """Compile one cluster into a synthesis row, honoring the idempotent write
    rule. `head` is the current synthesis for this topic (or None if first-ever).

    1. cluster hash == head's recorded hash → skip (NO model call, no row).
    2. compile; prose byte-identical to head → skip.
    3. prose differs (or no head) → write a new synthesis; if a head exists,
       supersede it (never update in place).
    """
    key = cluster.key

    # (1) no-op gate: identical inputs since the head was compiled.
    if head is not None and head.cluster_hash == _cluster_hash(cluster, compiler.model):
        r = CompileResult(key, "skipped-hash", synthesis_id=head.id)
        stats.record(r)
        return r

    prose = compiler.compile(cluster)
    if not prose:  # fail-open: a compiler failure must not abort the batch.
        r = CompileResult(key, "compile-failed")
        stats.record(r)
        return r

    # (2) inputs changed but prose is byte-identical → nothing to record.
    if head is not None and prose_hash(prose) == head.prose_hash:
        r = CompileResult(key, "skipped-identical", synthesis_id=head.id)
        stats.record(r)
        return r

    meta = build_metadata(cluster, compiler, prompt_memory_id)
    conf = min_member_confidence(cluster)
    title = cluster.members[0].display_title
    embed_text = f"{title}\n\n{prose}"  # topic label in-vector (S4)

    if head is None:
        if _write is None:
            from memory.write import memory_write_impl as _write
        new_raw = await _write(
            type="synthesis", content=prose, title=title,
            metadata=json.dumps(meta), confidence=conf,
            importance=_cluster_importance(cluster), embed=True,
            embed_text=embed_text, scope=scope, user_id=user_id,
            source="wiki-compile",
        )
        r = CompileResult(key, "created", synthesis_id=_extract_id(new_raw))
    else:
        if _supersede is None:
            from memory.write import memory_supersede_impl as _supersede
        new_raw = await _supersede(
            old_id=head.id, type="synthesis", content=prose, title=title,
            metadata=json.dumps(meta), importance=_cluster_importance(cluster),
            embed=True, embed_text=embed_text, scope=scope or "", user_id=user_id,
            source="wiki-compile",
        )
        r = CompileResult(key, "superseded",
                          synthesis_id=_extract_id(new_raw), superseded_id=head.id)
    stats.record(r)
    return r


def _cluster_importance(cluster: Cluster) -> float:
    """Max member importance (the wiki's existing prominence signal)."""
    return max((m.importance or 0.0) for m in cluster.members) if cluster.members else 0.0


def _extract_id(raw: object) -> str:
    """memory_write_impl / memory_supersede_impl return the new id, sometimes
    wrapped ("Created: <uuid>" / a bare id). Normalize to the id."""
    s = str(raw or "")
    for marker in ("Created: ", "Superseded: ", "-> "):
        if marker in s:
            s = s.split(marker, 1)[1]
    return s.strip().strip('"').split()[0] if s.strip() else s


@dataclass
class ExistingSynthesis:
    """The current (head) synthesis for a topic, loaded from the store so the
    writer can decide skip vs. supersede without re-deriving it."""
    id: str
    cluster_hash: str
    prose_hash: str
