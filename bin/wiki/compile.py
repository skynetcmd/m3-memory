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
from typing import Iterable, Optional, Protocol

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
    demoted: int = 0  # multi-member clusters the admission gate sent to orphans
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

    @property
    def admitted(self) -> int:
        """Clusters that cleared the admission gate and reached the compiler —
        whatever the per-cluster outcome (created / superseded / skipped / failed).
        The retry wrapper uses (admitted == 0 and demoted > 0) as its cold-corpus
        signal: every cluster existed but the gate sent them all to orphans."""
        return (self.created + self.superseded + self.skipped_hash
                + self.skipped_identical + self.failed)

    def summary(self) -> str:
        demoted = f", {self.demoted} demoted (weak-anchor)" if self.demoted else ""
        return (f"compile: {self.created} created, {self.superseded} superseded, "
                f"{self.skipped_hash + self.skipped_identical} skipped "
                f"({self.skipped_hash} unchanged, {self.skipped_identical} identical), "
                f"{self.failed} failed{demoted}; prompts {self.prompts_written} written / "
                f"{self.prompts_reused} reused")


# ── pure helpers (deterministic, unit-tested without a model) ─────────────────

def source_members(cluster: Cluster) -> list:
    """Cluster members that are legitimate SOURCES for a synthesis — i.e. NOT
    other syntheses.

    A synthesis must never be compiled from its own kind: feeding compiled prose
    back in as a source is how summary-creep compounds (an early omission becomes
    an input to the next compile), the exact failure glukhov names. This bit the
    real corpus — a recompile fed a prior synthesis of the SAME topic back in.
    All source-facing reads (the prompt, member_ids manifest, confidence, edges)
    go through this so the exclusion is applied in exactly one place.

    Falls back to all members only if the cluster is ENTIRELY syntheses (nothing
    else to compile from) — better to re-summarize than to produce nothing.
    """
    srcs = [m for m in cluster.members if m.type != "synthesis"]
    return srcs if srcs else list(cluster.members)


def min_member_confidence(cluster: Cluster) -> float:
    """MIN of source-member confidences, NULLs excluded; all-NULL → constant.

    A synthesis is only as trustworthy as its weakest source. Never returns 1.0
    from members alone here — callers may still cap, but the rule is a floor, not
    a promotion.
    """
    vals = [m.confidence for m in source_members(cluster) if m.confidence is not None]
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
    members = source_members(cluster)  # manifest excludes prior syntheses
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
    _write=None, _lookup=None,
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
    existing = _find_prompt_by_title(title, _lookup=_lookup)
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


def _find_prompt_by_title(title: str, *, _lookup=None) -> Optional[str]:
    """Return the id of an existing compiler-prompt row with this EXACT title, or
    None. An exact-equality SQL lookup through the seam — NOT a semantic search.

    Semantic search was wrong here: memory_search_impl ranks + returns a large
    result set (and, on this store, degrades to matching everything for a
    bracket-heavy FTS query), and its items are not the shape a title-equality
    check needs. Dedup must be exact and deterministic, so we query
    memory_items.title = ? directly, live (not is_deleted) so a superseded/erased
    prompt is re-created rather than reused. Placeholder comes from the dialect —
    no backend branch.
    """
    if _lookup is not None:  # test seam
        return _lookup(title)
    try:
        from memory.backends import dialect
        from memory.db import _db
    except Exception:
        return None
    try:
        p = dialect().param()
        with _db() as conn:
            row = conn.execute(
                f"SELECT id FROM memory_items "
                f"WHERE title = {p} AND is_deleted = 0 "
                f"ORDER BY id LIMIT 1",
                (title,),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    # Row may be a tuple or a name-access row depending on backend/factory.
    try:
        return row["id"]
    except (KeyError, IndexError, TypeError):
        return row[0]


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
    _link=None,
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
    if prose:
        # Scrub PII from the COMPILED PROSE before it becomes durable (§6: content
        # safety at the write boundary). The synthesis is a shareable, redacted
        # VIEW; the linked source memories keep full fidelity as the system of
        # record. Redaction is enforced here in code, not trusted to the prompt —
        # the v1/v2/v3 A/B proved a local model leaks paths/usernames verbatim
        # regardless of instruction. Also feeds the prose_hash below, so the
        # idempotence gate keys on the scrubbed text (stable across runs).
        from .pii_scrub import scrub_prose
        prose, _n = scrub_prose(prose)
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

    # Write the provenance edges the whole design depends on: a `consolidates`
    # edge from the new synthesis to every source member. Without these the
    # synthesis is a graph ORPHAN — it clusters apart from its sources, its prose
    # never reaches the source topic page, and blast-radius / citation-drift (which
    # walk these edges) see nothing. metadata.member_ids is the manifest; these
    # edges are the graph. (Bug found in live review 2026-07-24: the writer
    # recorded member_ids but never wrote the edges.)
    if r.synthesis_id:
        await _write_provenance_edges(r.synthesis_id, cluster, _link=_link)
    stats.record(r)
    return r


async def _write_provenance_edges(synthesis_id, cluster, *, _link=None):
    """`consolidates` edge from a synthesis to each of its source members.
    Best-effort per edge: one failed link must not abort the compile pass."""
    if _link is None:
        from memory.write import memory_link_impl as _link
    for m in source_members(cluster):  # never cite a prior synthesis as a source
        if m.id == synthesis_id:
            continue  # never self-link
        try:
            res = _link(synthesis_id, m.id, "consolidates")
            if hasattr(res, "__await__"):
                await res
        except Exception:
            pass


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


def load_heads(mem_rows: "Iterable") -> dict[str, "ExistingSynthesis"]:
    """Index live synthesis rows by TITLE → their head ExistingSynthesis.

    A synthesis's title is stable across supersessions (compile.py sets it from
    the cluster's top member), so title is the topic key that lets the writer
    find "the current synthesis for this cluster". `mem_rows` are dict-like rows
    (id, title, content, metadata dict) already filtered to live syntheses by the
    caller — this function does no I/O, so it is unit-testable.

    When two live syntheses share a title (should not happen — supersede closes
    the old one — but be defensive), the one whose id sorts last wins
    deterministically.
    """
    out: dict[str, ExistingSynthesis] = {}
    prev_id: dict[str, str] = {}
    for r in mem_rows:
        title = r.get("title") or ""
        rid = r.get("id") or ""
        if title in out and rid <= prev_id.get(title, ""):
            continue
        meta = r.get("metadata") or {}
        compiler = meta.get("compiler") or {}
        out[title] = ExistingSynthesis(
            id=rid,
            cluster_hash=compiler.get("cluster_hash", ""),
            prose_hash=prose_hash(r.get("content") or ""),
        )
        prev_id[title] = rid
    return out


def _admit(clusters, edges, gate, stats: CompileStats) -> list:
    """Filter clusters through the admission gate, recording demotions on `stats`.

    No `edges` → gating disabled (returns the clusters unchanged): the anchor
    needs the edge graph to score, and callers that don't supply it opt out. A
    demoted cluster is simply dropped from the compile set; it falls to the orphan
    list at render time (its members are never lost, only un-synthesized).
    """
    clusters = list(clusters)
    if edges is None:
        return clusters
    from . import admission
    gate = gate or admission.load_gate()
    if not gate.enabled:
        return clusters
    admitted = []
    for c in clusters:
        if len(c.members) < 2:
            admitted.append(c)
            continue
        if gate.admits(c, edges):
            admitted.append(c)
        else:
            stats.demoted += 1
    return admitted


async def compile_clusters(
    clusters: "Iterable",
    compiler: ProseCompiler,
    heads: dict[str, "ExistingSynthesis"],
    *,
    scope: str = "agent",
    user_id: str = "",
    backend=None,
    checkpoint_every: int = 500,
    edges: "Optional[list]" = None,
    gate=None,
    importance_threshold: "Optional[float]" = None,
    entity_comention: bool = False,
    record_run: bool = True,
    _write=None,
    _supersede=None,
    _link=None,
    _ensure_prompt=None,
    _ledger_db=None,
) -> CompileStats:
    """Orchestrate a full compile pass over many clusters. The I/O-bearing entry
    point the CLI calls; compile_cluster is the per-cluster decision.

    Persists the compiler's prompt ONCE (deduped), then walks the clusters in a
    single deterministic pass (no recursion, fixed list — cannot loop). Heads are
    keyed by title. WAL is bounded via the seam's maintenance_checkpoint every
    `checkpoint_every` writes (§10), never a direct PRAGMA. Best-effort
    checkpoint — a housekeeping failure never aborts the pass.

    ADMISSION GATE (§ cluster admission): when `edges` is supplied, each
    multi-member cluster is scored with KAS and run through the admission gate
    BEFORE the model call. A weakly-anchored cluster (co-mention grab-bag or
    thin-provenance mega-page) is demoted — skipped here, so it falls to the
    orphan list at render time instead of shipping as a low-quality synthesis.
    Demoting before the model call also saves its compile cost. Pass `gate` to
    override the config-resolved default; pass no `edges` to disable gating
    entirely (the pre-gate behaviour, e.g. for an audit pass).

    LEDGER (plan G1): when `record_run` is True, the pass opens a `cluster_run`
    provenance row, FREEZES each admitted cluster's membership into
    `cluster_members` (UUIDs only), and stamps the run complete at the end. Frozen
    membership is what lets a later pass recognize an unchanged topic WITHOUT
    recomputing its clustering — recompute is what perturbs it (the residual 2/13
    idempotence churn). The ledger is best-effort: any failure logs and the compile
    proceeds — it is an audit/optimization surface, never on the critical path of a
    correct single pass. Pass `_ledger_db` (a live seam connection) in tests;
    otherwise the pass opens one via the memory seam.
    """
    stats = CompileStats()
    ensure_prompt = _ensure_prompt or _ensure_prompt_row
    prompt_id = await ensure_prompt(compiler, stats, scope=scope, user_id=user_id)

    clusters = _admit(clusters, edges, gate, stats)

    # Ledger: open a provenance run (invisible until completed) and collect the
    # membership rows as we go. All best-effort — a ledger failure must not abort.
    run_ctx = _LedgerRun(record_run, _ledger_db, scope=scope, user_id=user_id,
                         importance_threshold=importance_threshold,
                         entity_comention=entity_comention)
    run_ctx.open()

    writes_since_ckpt = 0
    for cluster in clusters:
        title = cluster.members[0].display_title if cluster.members else ""
        head = heads.get(title)
        r = await compile_cluster(
            cluster, compiler, head, prompt_id, stats,
            scope=scope, user_id=user_id, _write=_write, _supersede=_supersede,
            _link=_link)
        run_ctx.add(cluster, compiler, r)
        if r.action in ("created", "superseded"):
            writes_since_ckpt += 1
            if backend is not None and writes_since_ckpt >= checkpoint_every:
                _try_checkpoint(backend, final=False)
                writes_since_ckpt = 0
    if backend is not None:
        _try_checkpoint(backend, final=True)

    run_ctx.complete(len(clusters))
    return stats


class _LedgerRun:
    """Best-effort accumulator that records a compile pass into the G1 ledger.

    Isolates every ledger touch behind try/except so the compile pass is never
    aborted by a ledger problem (a missing table on an un-migrated DB, a seam
    hiccup). Collects `(cluster_key, memory_id, cluster_hash, head_synthesis_id)`
    per admitted cluster, then writes them all + completes the run at the end.
    """

    def __init__(self, enabled, db, *, scope, user_id, importance_threshold,
                 entity_comention):
        self.enabled = bool(enabled)
        self._db_arg = db
        self._scope = scope
        self._user_id = user_id
        self._tau = importance_threshold
        self._comention = entity_comention
        self._db = None
        self._dialect = None
        self._own_db = False
        self.run_id = None
        self._rows: list = []

    def _resolve(self):
        if self._db_arg is not None:
            self._db = self._db_arg
            from memory.backends import dialect
            self._dialect = dialect()
            return True
        from memory.backends import dialect
        from memory.db import _db
        self._own_cm = _db()
        self._db = self._own_cm.__enter__()
        self._own_db = True
        self._dialect = dialect()
        return True

    def open(self):
        if not self.enabled:
            return
        try:
            from . import ledger
            self._resolve()
            self.run_id = ledger.open_run(
                self._db, self._dialect, scope=self._scope, user_id=self._user_id,
                importance_threshold=self._tau, entity_comention=self._comention)
        except Exception as e:  # pragma: no cover - defensive
            _log_ledger("open", e)
            self.enabled = False

    def add(self, cluster, compiler, result: CompileResult):
        if not self.enabled or self.run_id is None:
            return
        try:
            from .synth import _cluster_hash
            chash = _cluster_hash(cluster, compiler.model)
            head_id = result.synthesis_id
            for m in cluster.members:  # freeze ALL members (source + synthesis)
                self._rows.append((cluster.key, m.id, chash, head_id))
        except Exception as e:  # pragma: no cover - defensive
            _log_ledger("add", e)

    def complete(self, cluster_count: int):
        if not self.enabled or self.run_id is None:
            return
        try:
            from . import ledger
            ledger.record_members(self._db, self._dialect, self.run_id, self._rows)
            ledger.complete_run(self._db, self._dialect, self.run_id,
                                cluster_count=cluster_count)
            # Generational cleanup: keep only this run's cached membership warm.
            ledger.prune_superseded_members(self._db, self._dialect, self.run_id)
            if hasattr(self._db, "commit"):
                self._db.commit()
        except Exception as e:  # pragma: no cover - defensive
            _log_ledger("complete", e)
        finally:
            if self._own_db and getattr(self, "_own_cm", None) is not None:
                try:
                    self._own_cm.__exit__(None, None, None)
                except Exception:
                    pass


def _log_ledger(phase: str, e: Exception) -> None:
    import logging
    logging.getLogger("m3.wiki.ledger").warning(
        "compile ledger %s failed (non-fatal): %r", phase, e)


def _try_checkpoint(backend, *, final: bool) -> None:
    """Bound the WAL via the seam. Best-effort (§10): never aborts the batch."""
    try:
        conn_cm = backend.connection()
    except Exception:
        return
    try:
        with conn_cm as conn:
            backend.maintenance_checkpoint(conn, final=final)
    except Exception:
        pass
