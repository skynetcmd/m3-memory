# Confidence, Trust & Knowledge Maintenance

M3 treats stored memories not as a flat retrieval index but as a body of
knowledge that is *maintained*: facts carry a **confidence**, sources carry a
**trust**, agreement and disagreement are recorded, confidence **reinforces and
decays** over time, and large clusters of episodic memories **consolidate** into
stable beliefs. Every part of this is **additive and off by default** — nothing
about write or retrieval behavior changes until you opt in.

> Implementation plan & phase history: `docs/plans/KNOWLEDGE_MAINTENANCE_PLAN.md`.
> All runtime flags: `docs/ENVIRONMENT_VARIABLES.md` → *Knowledge Maintenance*.

## The confidence model (hybrid)

Each memory can carry two numbers (columns on `memory_items`, migration 035):

1. **`confidence`** ∈ [0, 1] — the **transparent, user-facing** value. A
   provenance prior plus a diminishing corroboration bonus minus a capped
   contradiction penalty, all clamped. Inspectable and explainable. This is what
   `memory_get` / `memory_search` surface.
2. **`belief_alpha` / `belief_beta`** — an optional **Beta(α,β) posterior** kept
   alongside for ranking experiments (`M3_CONFIDENCE_MODEL=bayesian`). Never the
   displayed number.

A **NULL** confidence means "not derived yet" and is treated everywhere as *fall
back to `importance`* — so legacy rows and un-enriched writes behave exactly as
before.

### Transparent aggregation

```
confidence = clamp01(
    base_source_conf                       # where it came from
  + corroboration_bonus(distinct_trust_sum)  # diminishing, capped at +0.20
  - contradiction_penalty(contradictions)    # capped at -0.30
)
```

| Provenance | Base prior |
|---|---|
| user statement / `change_agent=manual` | 0.95 |
| Observer-SLM observation | the SLM's own 0.6–1.0 |
| agent assertion | 0.70 × that agent's trust |
| internet / web research | 0.40 |
| unknown | 0.50 (= `importance` default — neutral) |

Caps are deliberate: three agreeing agents (bonus ≤ 0.20 on a 0.70 base = 0.90)
still rank below a bare user statement (0.95). Pure code in
`bin/memory/confidence.py`, exhaustively unit-tested.

## Trust & corroboration (migration 036)

- **`agents.trust_score`** ∈ [0.5, 1.0], default 1.0 (neutral). Weights an
  agent's assertions. Set explicitly with the **`agent_set_trust`** MCP tool;
  auto-tuning is deferred (`M3_TRUST_AUTOTUNE`, off).
- **`memory_corroborations`** — an append-only ledger of who corroborated or
  contradicted each memory (with the source's trust frozen at write time). It is
  the source of the aggregation inputs, so confidence is never re-derived by
  scanning on read.

**Corroboration on write** (`M3_CORROBORATION=1`, off by default): a near-identical
re-write (cosine ≥ `CORROBORATION_THRESHOLD` and same content) *corroborates the
existing memory* — bumping its `corroboration_count`/`confidence` and recording a
ledger event — instead of leaving an orphan duplicate. Because consensus is
cross-agent, the corroboration scan is agent-agnostic; contradiction/supersession
keeps its original same-agent semantics.

## Reinforcement — confidence as a living signal

The daily maintenance pass (`memory_maintenance.py`) makes confidence move:

- **Ledger-active memories** are re-aggregated from their current
  corroboration/contradiction record.
- **Un-reinforced memories** (no ledger activity, not accessed in 7 days) **decay
  toward NEUTRAL (0.5)** — not toward 0. A fact nobody reconfirms forgets toward
  *uncertainty*, not worthlessness. A corroborated floor is never crossed.
  Iterating converges to NEUTRAL without oscillation.
- **Access** is weak, log-bucketed, hard-capped positive evidence — being read a
  lot can never masquerade as being corroborated.

## Belief consolidation — episodic → semantic

`bin/consolidate_beliefs.py` (weekly cron, `M3_CONSOLIDATION_AUTO=1` to enable)
rolls up aged `observation` groups into high-order **`belief`** memories via the
local LLM. A belief carries high confidence and `consolidates` edges back to its
sources; the sources are **soft-deleted, never purged**, so every belief is
reversible and its provenance reconstructable. Protected types
(preference/user_fact/task/plan) are never consolidated; the job is dry-run unless
*both* `--apply` and the env flag are set.

## Confidence in retrieval ranking

With **`M3_CONFIDENCE_RANKING=1`** (off by default), a memory's `confidence` is
blended into the retrieval score as an additive term, weighted by
`M3_CONFIDENCE_WEIGHT` (0.10) — exactly like `IMPORTANCE_WEIGHT`. A NULL
confidence falls back to `importance`, so un-derived rows are unaffected.

**Zero-regression contract:** with the flag off, ranking is byte-identical to a
build without this feature — proven by `tests/test_confidence_ranking.py`
(equal-relevance rows score identically off, and confidence breaks ties only when
on) and the full flag-off search suite. Effectiveness on a controlled corpus:
`tests/test_confidence_ranking_effectiveness.py`. A production recall@10 on a
realistic multi-session corpus belongs with the LongMemEval bench harness, not the
shipped tests.

## Explainable retrieval — "why did you remember this?"

Retrieval shows its work. `memory_suggest` is `memory_search(..., explain=True)`, and
under explain mode every result carries both a **plain-English reason** and the exact
**numeric breakdown** it was synthesized from — so a false-positive is debuggable and a
recall is trustable, not a black box.

Real output (`memory_search(query, explain=True)`):

```
1. [b1942232-…] score=0.6765  type: knowledge  title: v2 multi-agent-team …
   Why: moderate semantic match; title overlaps the query; high importance
   Breakdown: vector=0.5635 (weight 0.70) + bm25=0.0725 (weight 0.30) -> raw=0.4162
   Importance: 0.6000
2. [917dc241-…] score=0.6398  type: chat_log  title: assistant@claude-code …
   Why: strong semantic match; title overlaps the query
   Breakdown: vector=0.6047 (weight 0.70) + bm25=0.0716 (weight 0.30) -> raw=0.4448
```

**How to read it:**
- **Why** — a human summary of the *dominant* signals (semantic strength, keyword/BM25
  hit, title overlap, importance, speaker/role match, and any intent routing such as
  "routed as temporal-reasoning"). Synthesized from the components below — no extra
  computation; explain-only.
- **Breakdown** — the raw hybrid math: `vector×w + bm25×(1−w) → raw`, plus MMR penalty
  (when diversification displaced a near-duplicate), recency/temporal boosts, and the
  final blended score after importance/confidence/role terms.

The reason string is generated by `_explain_reason` (pure, in `memory.search_ranking`);
the numeric dict is assembled in `memory_search_impl` under `explain=True`. Normal
(non-explain) search output is unchanged — the breakdown is opt-in via `explain` /
`memory_suggest`, never on the hot path.

## Quick start

```bash
# Confidence is derived automatically on every write (column added by migration
# 035). To see it:  m3 memory_get <id>   →  includes a `confidence` field.

# Opt in to the living-knowledge behaviors (each independent):
export M3_CORROBORATION=1          # near-dup writes corroborate, not duplicate
export M3_CONFIDENCE_RANKING=1     # confidence influences retrieval order
export M3_CONSOLIDATION_AUTO=1     # weekly belief consolidation actually writes

# Curate trust for a source:
#   agent_set_trust(agent_id="some-agent", trust_score=0.8)
```
