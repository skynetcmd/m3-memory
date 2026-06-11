# ADR-0001 — Materialized bypass-surface for rank-independent recall

- **Status:** Draft — open questions resolved 2026-06-11 (§10); ready for Accept on review
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

**Pre-registered before implementation — TWO bars, both binding (§5):**

> **(a) Effectiveness bar (absolute).** The materialized surface must lift recall over the
> ranked-only baseline by a pre-registered absolute margin, with **zero regression** on
> question classes where the policy is off. The absolute thresholds (baseline, target
> mean-recall, target % all-turns-surfaced) are **fixed and binding**, recorded in the
> internal bench report (held privately for bench-data discipline; not reproduced here).
> This is the falsifiable "does it actually work" gate — it is NOT optional just because
> the numbers live elsewhere.
>
> **(b) Reproduction bar.** On the same corpus the materialized read must **reproduce the
> validated per-query path within ±0.5pp** on both recall measures. This guards that the
> table-driven read is faithful to the function it replaces — a wrong table fails here
> even if it happens to clear (a).
>
> Merge requires BOTH: clears the absolute effectiveness bar AND matches the per-query
> path within ±0.5pp. Miss either → does not merge.

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

## 10. Decisions (resolved 2026-06-11)

The five open questions, each resolved against the tenets and grounded in existing core
patterns (not assumption):

1. **Read-path placement → RESOLVED: a flag on the routed search, not a new tool.**
   The read folds into `memory_search_routed_impl` (`bin/memory/search.py`) as
   `bypass_surface: bool = False`. No new MCP tool ⇒ no catalog growth, no manifest-regen
   churn, no `test_tool_count_drift` gate to satisfy (§12). The flag, when set, does a
   scoped indexed seek and unions the result into the routed hits (low-score, additive,
   never displacing ranked results). Re-exported through the `memory_core` shim,
   identity-preserving (§2).
   - **Scope isolation (§7):** the bypass seek **inherits the caller's exact scope
     predicate** — the same `conversation_id IN (…) AND scope = ? [AND user_id = ?]` the
     routed search already enforces. When the routed search spans multiple conversations
     (multi-db / org scope), the bypass seek carries the *same* set, never a broader one.
     It is never a single un-scoped `WHERE turn_id …` lookup. Empty/missing scope raises
     (it inherits the routed path's mandatory-scope contract — §7), so bypass cannot widen
     the blast radius of a query beyond what the caller was already authorized to see.

2. **Build trigger → RESOLVED: explicit builder with caller-supplied dirty-scope IDs;
   NEVER inline on write.** A `build_bypass_surface(conversation_ids=None, scope=…,
   cap=…)` in `bin/memory/entity.py` (CLI + callable). Two modes, one code path:
   - **Full build:** `conversation_ids=None` → (re)build every scope. First-run path.
   - **Incremental rebuild:** the caller passes the **list of changed scope IDs / PKs**
     (`conversation_ids=[…]`); the builder rebuilds *only those* — deletes their existing
     `bypass_surface` rows (`DELETE … WHERE conversation_id IN (…)`) and re-inserts. This
     is the steady-state path: whoever mutated the entities (an ingest job, an enrichment
     pass, a curation run) already knows which scopes it touched, so it hands that list to
     the builder. **The builder does NOT auto-detect change** — change-detection lives with
     the caller that caused the change. This keeps surfacing off the hot write path (§8)
     while making incremental updates an O(changed-scopes) operation, not a full rebuild.
   An inline hook on entity write is rejected — it would put surfacing work on the write
   path and blow the §8 latency budget. The surface is derived data; it lags a write until
   the next (incremental or full) builder call.

3. **Observation source dependency → RESOLVED: conditional, entity-half always present.**
   `source IN ('entity','observation')`. The entity half is always buildable (entity
   tables always exist). The observation half is built **only where the observer /
   fact-enrichment layer ran** for that scope (it is opt-in via `M3_ENABLE_FACT_ENRICHED`,
   per ARCHITECTURE §enrichment). The builder writes `obs` rows only for scopes with
   observations; absence is normal, not an error (§3 empty≠error). Documented so a
   deployment without enrichment still gets the entity-surface lift.

4. **Cap semantics → RESOLVED: `M3_BYPASS_SURFACE_CAP` env var, default 300.** The
   per-question cap is read from `M3_BYPASS_SURFACE_CAP` (matching the
   `M3_EMBED_CHUNK_MAX_CHARS` / `M3_ENABLE_ENTITY_GRAPH` convention in `bin/memory/`),
   **defaulting to 300** (the prototype's validated operating point). A per-strategy
   multiplier in the policy scales it (aggressive class = full cap, conservative = a
   fraction); nothing hardcoded. The resolved cap is stored on each surface row (`cap`
   column) so a build is auditable and a later build at a different cap is detectable.
   - **Read-time vs build-time cap:** the table stores the already-capped set per scope.
     A read-time request for a *lower* cap truncates the stored set (cheap, no rebuild); a
     *higher* read-time cap cannot exceed what was built — it requires a rebuild at the
     higher cap. The read path therefore never silently under-delivers: if the effective
     read cap > built cap, it surfaces what's stored and is a no-op beyond it (documented,
     not a silent ceiling — §3 fail-loud spirit: log when a read cap is clamped to built).
   A module `MAX` constant bounds pathological env inputs (§4 result-set caps).

5. **Migration down-path → RESOLVED: `DROP TABLE` is correct — confirmed by precedent.**
   The closest analog, `entity_embeddings` (migration 032), is a derived store-once table
   whose `032_…down.sql` is exactly `DROP TABLE IF EXISTS entity_embeddings;`. The
   bypass-surface is likewise pure derived data (rebuildable from entities + observations),
   so `033_…down.sql` = `DROP TABLE IF EXISTS bypass_surface;`. No data loss on rollback.

### Correction to §7 (GDPR) discovered while resolving Q5

`entity_embeddings` uses `REFERENCES entities(id) ON DELETE CASCADE`, and ADR drafts
assumed `bypass_surface` could likewise rely on FK cascade for `gdpr_forget`. **It cannot
rely on cascade alone.** The actual `gdpr_forget` implementation
(`bin/memory_maintenance.py`) purges by an **explicit enumerated list** of
`DELETE FROM <table> WHERE …` (memory_embeddings, memory_relationships, chroma_sync_queue,
memory_history, memory_items) — it does not depend on SQLite cascade firing. Therefore
**`bypass_surface` MUST be added to that explicit `gdpr_forget` table list** (a `DELETE
FROM bypass_surface WHERE conversation_id IN (…)` / `WHERE user_id = ?`), in addition to
an FK for defense-in-depth. This is a concrete merge requirement, not optional — without
it, erased users' surfaced turn pointers would persist. (§9 transitivity is satisfied by
explicit enumeration, per the repo's actual pattern.)

## 11. Validation plan (before merge)

1. Build the surface on the internal benchmark corpus; confirm the read path reproduces the per-query
   bench numbers within ±0.5pp (§4 pre-reg).
2. `EXPLAIN QUERY PLAN` on the read path shows the index seek (§8).
3. P50/P95/P99 within budget via `bench_memory.py` (§8).
4. Behavior baseline captured + parity test green (§3, §11).
5. `gdpr_forget` on a test user removes its surface rows — via the explicit
   `DELETE FROM bypass_surface` added to `memory_maintenance.gdpr_forget` (NOT cascade
   alone; see §10 correction) (§9).
6. Scope-isolation test: a query for conversation A never returns B's turns (§7).

---

*Authority for this spec: `DESIGN_PHILOSOPHIES.md`. Experimental validation was performed
on an internal retrieval benchmark held privately; the detailed validation record and
absolute numbers live with that benchmark. This core spec carries only the design — no
benchmark data, numbers, paths, or per-question content.*
