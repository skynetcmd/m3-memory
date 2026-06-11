# ADR-0001 — Materialized bypass-surface for rank-independent recall

- **Status:** Draft (review before implementation)
- **Date:** 2026-06-11
- **Authority:** `../DESIGN_PHILOSOPHIES.md` (the seven tenets)
- **Validation:** prototyped and measured on an internal retrieval benchmark (held
  privately). This ADR carries only the design and qualitative outcome — no benchmark
  data, numbers, paths, or per-question content.
- **Supersedes / superseded by:** none.

> Sections map to the seven tenets so the decision is reviewable against each. Code does
> not merge ahead of this ADR being **Accepted** and the §4 metric target agreed.

## 0. Problem & one-line proposal

**Problem.** Pure relevance ranking (top-k) misses answer-bearing turns that exist in
the store but rank below the cut — enumeration/aggregation/confirmation questions
especially, whose individual instance turns don't lexically resemble the question.

**Proposal.** A **materialized surface table** that precomputes, per scope, the turns
worth surfacing *by typed atom* (rank-independently), so the retrieval path adds them
with a single indexed seek instead of recomputing surfacing per query. On an internal
benchmark this materially lifts recall of the otherwise-missed turns, concentrated on the
enumeration/aggregation question class, with **zero regression** on classes where the
gating leaves it off.

Validated experimentally as a per-query function. This spec is about making it a
**scope-correct, indexed, offline-built core capability** — because per-query surfacing is
O(Q) work + a full scan per run, the wrong shape at corpus scale (the reason this is a
spec, not a direct port of the prototype).

---

## 1. Local-first (§1)

- Pure SQLite. No external calls. The builder reads existing entity/observation tables
  and writes one new table in the same L1 DB. Runs identically on a laptop or air-gapped.
- No telemetry, no egress. The surface is derived data, fully reproducible offline.

## 2. Modularity (§2)

- **Read path** lives in `bin/memory/search.py` as an opt-in leg of the existing routed
  search (`memory_search_routed(..., bypass_surface=True)`), re-exported through the
  `memory_core` shim via `from .search import …` (identity-preserving, no rebind).
- **Builder** lives in `bin/memory/entity.py` (it's an entity-derived surface), exposed
  as `build_bypass_surface(conversation_ids=None, scope=…, cap=…)`. Lazy-imports
  `_track_cost`/`_db` inside the function body (cycle-breaking per §2).
- **One feature per PR.** PR = migration 033 + builder + read-path flag + tests. Nothing
  else rides along.
- The strategy→atom-type policy is the existing `strategy_router` output, not a new
  classifier — we consume the production router (works on any question text), we don't
  fork it.

## 3. Robustness (§3)

- Builder and read path return **structured rows** (`{turn_id, source, …}`), never
  strings. Empty surface = empty list, never None.
- **Crash on contract violation:** empty/missing `conversation_id` (or `scope`) raises —
  never defaults to a global scan (this is also §7).
- **Cursor discipline:** the builder iterates scopes with a **separate cursor** (or
  `.fetchall()` first) — never reuses one cursor for outer-scope iteration + inner
  per-scope queries (the 2026-06-09 fake-uniform-count incident, cited in §3). Any
  per-scope count that comes back suspiciously uniform is treated as guilty until
  spot-checked against a direct query.
- A **bulk read variant** exists from day one (surface for a list of scopes in one call),
  so no agent loops single-scope calls (§3 / §4).

## 4. Effectiveness — pre-registered metric (§5)

**Pre-registered before implementation:**

> On the internal retrieval benchmark (the §8 representative corpus), the materialized
> surface must **reproduce the validated per-query path within ±0.5pp** on both recall
> measures (mean recall and % of questions with all answer-bearing turns surfaced), with
> **zero regression** on question classes where the policy is off (recall unchanged vs.
> base, exact). If the materialized path does not match the per-query path within ±0.5pp,
> it does not merge — the table is wrong, not the metric. (Absolute targets are recorded
> in the internal bench report, not here.)

- Behavior baseline: extend `tests/capture_retrieval_baseline.py` with N deterministic
  bypass-surface queries, byte-fingerprinted, so future refactors can't silently drift.
- Negative-result honesty: if a strategy shows no lift, it ships *off* by policy and that
  is documented (per §5; mirrors the bench where ASSISTANT/PROSE are off by design).

## 5. Hardening (§6)

- **Parameterized SQL only**, including the scope range bounds. No string interpolation.
- **Read-only read path:** the search-time consumer only SELECTs from the surface table.
- **Builder is the only writer**, semaphore-bounded if it embeds (it does not — it reads
  existing entity/embedding tables), and it is **gated** (`default_allowed=False` if
  exposed as an MCP tool, since it writes/rebuilds a table).
- Audit: a `built_at` + `source_run` column per row; a full rebuild logs to history.

## 6. Privacy & multi-tenancy (§7) — the biggest delta from the bench prototype

The prototype scoped by a bare conversation prefix with a free `LIKE`. **Core must not.** The
table carries the standard scope columns and every read/build query filters on them:

- `conversation_id` (**mandatory**, raises if empty), `user_id`, `scope`
  (`agent`/`user`/`session`/`org`).
- Per-conversation isolation is **baked into the SQL `WHERE`**, not enforced app-side.
- The surface stores **turn_ids only (no content)** — the consumer fetches text through
  the existing authz'd read path (§7: aggregation returns IDs, content reads stay on the
  one checkpoint).
- **`gdpr_forget` transitivity:** the surface is derived from entity tables; on erasure
  the upstream rows vanish and the surface must be rebuilt (or carry an
  `ON DELETE CASCADE` FK to the source). Spec'd as: surface rows FK-cascade from the
  entity/turn they point at, so erasure naturally removes them — no separate wiring (§9).

## 7. Performance (§8) — indexed, EXPLAIN-validated, budgeted

**Schema (migration 033, DRAFT):**

```sql
CREATE TABLE bypass_surface (
    conversation_id TEXT NOT NULL,
    turn_id         INTEGER NOT NULL,
    source          TEXT NOT NULL,          -- 'entity' | 'observation'
    strategy        TEXT,                   -- the router strategy this was built under
    user_id         TEXT,
    scope           TEXT NOT NULL DEFAULT 'agent',
    cap             INTEGER,
    built_at        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (conversation_id, turn_id)
);
CREATE INDEX idx_bypass_surface_scope ON bypass_surface(conversation_id, scope, user_id);
```

- **Hot path = a single covering seek:** `SELECT turn_id, source FROM bypass_surface
  WHERE conversation_id = ? AND scope = ?`. `EXPLAIN QUERY PLAN` must show
  `SEARCH … USING INDEX idx_bypass_surface_scope` — gated before merge.
- **Add the index only because the bench shows the join is the bottleneck** (§8): the
  per-query prototype's entity surface, even after adding `idx_mie_memory_id` + `ANALYZE`,
  was a range-scan-then-join per question; materializing removes the join from the hot
  path entirely. **The materialized read is the justification for not adding more indexes
  on the source tables** (premature indexing costs write throughput).
- **Budget:** read path P50 < 5 ms / P95 < 20 ms / P99 < 50 ms on a ~250k-mention
  representative corpus (the §8 numbers). The builder is a batch op: worst-case wall-clock budget stated
  per corpus size; **`PRAGMA wal_checkpoint(PASSIVE)` every ~1,000 rows**, `TRUNCATE` at
  clean exit (§10).
- **`ANALYZE` after build** so the planner has stats for the new table (the bench showed
  the planner picks the wrong index without it — documented lesson).
- **Idempotent + incremental:** rebuild only the scopes whose source entities changed
  since `built_at` (a dirty-scope set), not a full rebuild every time (§4 idempotent
  retries). Full rebuild is the fallback / first-run path.

## 8. WAL & DB hygiene (§10)

- New connection uses `apply_pragmas(profile_for_db(path))` — no inline PRAGMAs.
- Builder checkpoints PASSIVE every ~1k rows / 60s; TRUNCATE at clean exit.

## 9. Tool-shape (§12), if exposed via MCP

- If surfaced as a tool: structured rows, bulk variant, `default_allowed=False` for the
  builder (it writes). The read is folded into the existing search tool as a flag, so it
  needs no new tool — preferred (no catalog growth, no manifest regen churn).
- If a tool *is* added: regenerate `docs/tools/MCP_CATALOG.json` + `docs/MCP_TOOLS.md`,
  run `test_tool_count_drift` / `test_mcp_catalog_manifest_fresh`, update prose counts
  (per CLAUDE.md pre-push discipline).

## 10. Open questions for review

1. **Read-path placement:** a `bypass_surface=True` flag on `memory_search_routed`
   (preferred — no new tool), vs. a standalone surface read. → leaning flag.
2. **Build trigger:** explicit `build_bypass_surface()` call / CLI, vs. an incremental
   hook on entity write (heavier, risks write-path latency — §8 says no). → leaning
   explicit + incremental-dirty-scope, never inline on write.
3. **Observation source dependency:** the obs surface needs a consolidated-observation
   layer to exist for the scope. In core that's the optional fact-enrichment/observer
   output — so the obs half of the surface is **conditional on enrichment being enabled**;
   entity half always available. Spec'd as: `source IN ('entity','observation')`, obs rows
   only present where the observer ran.
4. **Cap semantics across scopes:** the prototype's per-question cap was tuned on the
   enumeration class. Core default + per-
   strategy override; expose as config (`M3_BYPASS_SURFACE_CAP`), not hardcoded.
5. **Migration down-path:** `DROP TABLE bypass_surface` is a clean down (derived data,
   no loss) — confirm that's acceptable vs. preserving.

## 11. Validation plan (before merge)

1. Build the surface on the internal benchmark corpus; confirm the read path reproduces the per-query
   bench numbers within ±0.5pp (§4 pre-reg).
2. `EXPLAIN QUERY PLAN` on the read path shows the index seek (§8).
3. P50/P95/P99 within budget via `bench_memory.py` (§8).
4. Behavior baseline captured + parity test green (§3, §11).
5. `gdpr_forget` on a test user removes its surface rows (FK cascade) (§9).
6. Scope-isolation test: a query for conversation A never returns B's turns (§7).

---

*Authority for this spec: `DESIGN_PHILOSOPHIES.md`. Experimental validation was performed
on an internal retrieval benchmark held privately; the detailed validation record and
absolute numbers live with that benchmark. This core spec carries only the design — no
benchmark data, numbers, paths, or per-question content.*
