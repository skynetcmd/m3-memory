# Knowledge Maintenance — Implementation Plan

> **Status:** ✅ ALL PHASES (0–5) shipped — flag-off, zero-regression. Confidence, trust-weighted/
> consensus provenance, reinforcement, autonomous belief consolidation, and flag-gated
> confidence ranking are all in place. See `docs/CONFIDENCE_AND_TRUST.md`.
> **Branch:** `feature/m3v3-m5-sqlite-vec-dep`.
> **Goal:** Move M3 from *memory retrieval* to *knowledge maintenance* by adding first-class
> **confidence**, **trust-weighted provenance**, **reinforcement**, and **autonomous
> episodic→semantic consolidation** — additively, behind flags, with zero regression to today's behavior.

## Pre-registered effectiveness metric (DESIGN_PHILOSOPHIES §5 — mandatory)

Confidence-as-a-feature only *does its job* if it improves retrieval when enabled. Per §5, the
metric and threshold are registered **before** Phase 5 writes ranking code:

- **Metric:** retrieval recall@10 (and contradicted-fact suppression rate) on a held-out fixture
  set where some facts are corroborated, some contradicted, some single-source.
- **Threshold to merge Phase 5's flag-on path:** `M3_CONFIDENCE_RANKING=1` must show **≥ +3pp
  recall@10** on the corroborated-fact subset **and** demote superseded/contradicted facts at least
  as well as today (no recall regression on the neutral subset). If it doesn't clear that bar, the
  flag stays off by default and ships as experimental-only.
- **Behavior baseline (§11):** `tests/capture_confidence_baseline.py` (mirrors
  `capture_retrieval_baseline.py`) is built as the **first task of Phase 5, before any ranking
  edit**, byte-fingerprinting N queries with the flag OFF. The Phase 5 gate is: flag-off output is
  byte-identical to that baseline. This makes "additive & backward-compatible" a tested contract,
  not a claim.

## Decisions locked (2026-06-27)

| Axis | Decision |
|---|---|
| **Scope** | All 4 gaps, built as sequenced phases with gates between them. |
| **Model** | **Hybrid** — transparent weighted aggregation is the stored/displayed `confidence`; an optional Bayesian (Beta) posterior is kept *alongside* for ranking experiments, never the user-facing number. |
| **Rollout** | **Additive & backward-compatible** — new columns default to neutral (`confidence = importance`, `trust_score = 1.0`); ranking changes gated behind `M3_CONFIDENCE_RANKING` (default off). Nothing regresses until explicitly enabled. |

## What already exists (verified in code — do NOT rebuild)

The audit undersold the engine. Grounded findings that shrink the build:

- **Observer SLM already emits a `confidence` float (0.6–1.0)** per observation, stored in
  `metadata_json.confidence` (`bin/run_observer.py:424-569`). Phase 1 *promotes* this to a
  column; it does not invent confidence from nothing.
- **`memory_consolidate_impl` already does episodic→semantic** — LLM-summarizes N same-type
  items into a `type='summary'` row, writes `consolidates` edges, soft-deletes originals, with
  safety gates (`stale_days`, `max_importance`, `protected_types`, `dry_run`)
  (`bin/memory_maintenance.py:538-666`). It is **built but never auto-invoked**. Phase 4 is mostly
  *triggering* it, not writing it.
- **Bitemporal `as_of` queries work end-to-end** (`bin/memory/search.py:1108-1114`). Reuse, don't touch.
- **Contradiction detection + supersession are live** (`_check_contradictions`,
  `_mark_superseded`, `memory_history`) (`bin/memory/write.py:1124-1224, 431-496`).
- **Access stamping is async-batched** (`access_count`, `last_accessed_at`, 0.25s flush)
  (`bin/memory/db.py:362-407`). The reinforcement loop (Phase 3) hooks this, doesn't replace it.
- **Scoring is additive** — all post-hybrid bonuses (`temporal`, `recency`, `role`) are `+` terms
  (`bin/memory/search.py:744-808, 1334, 1361`). A confidence term slots in the same way.
- **Agent registry exists** (`agents` table: `agent_id, role, status, capabilities, last_seen`)
  with `agent_register/heartbeat/list/get` (`memory/migrations/012`, `bin/memory_core.py:1654-1723`).
  **No `trust_score` column** — that is the genuine Phase 2 gap.

## Integration facts the plan is built on (cite these in PRs)

| Fact | Location |
|---|---|
| `memory_items` has NO `confidence` col; `importance REAL DEFAULT 0.5` | `memory/migrations/001:54` |
| Highest migration = **033**; next is **034** | `memory/migrations/033_bypass_surface.*` |
| Two migration dirs: `memory/migrations/` (authoritative for `memory_items`), `memory/chatlog_migrations/` (chatlog DB) | `bin/migrate_memory.py:71`, `bin/chatlog_config.py:84` |
| Migrations: `NNN_name.up.sql` + `NNN_name.down.sql`; down required for reversibility; tracked in `schema_versions` | `bin/migrate_memory.py:20-27,418,459-471` |
| Write INSERT choke-point (single place to set `confidence`) | `bin/memory/write.py:229-233` |
| Provenance known at write: `agent_id, model_id, change_agent, source, user_id, scope, variant` | `bin/memory/write.py:130-131,193` |
| `change_agent` inference + `VALID_CHANGE_AGENTS` | `bin/embedding_utils.py:175-196` |
| Same fact written twice → **2 live rows**, linked `related`, NOT merged/corroborated | `bin/memory/write.py:1219-1220` |
| Config flag pattern: `os.environ.get("M3_X","default")` as import-time constant | `bin/memory/config.py:153,181-182,222` |
| Ranking inject points (Rust + legacy paths) | `bin/memory/search.py:1334,1361` |
| Daily maintenance ops (decay `importance*=0.995` >7d, purge, archive, retention, VACUUM) | `bin/memory_maintenance.py:232-343` |
| Cross-platform scheduling (cron/launchd/systemd) | `bin/crontab.template`, `bin/install_schedules.py:40-147` |
| `VALID_MEMORY_TYPES` (no `belief`/`pattern` type today) | `bin/mcp_tool_catalog.py:65-83` |

---

# Architecture: the confidence model (Hybrid)

Two numbers per memory, both new columns on `memory_items`:

1. **`confidence REAL DEFAULT NULL`** — the **transparent, displayed** value in `[0,1]`.
   Deterministic, inspectable, testable. This is what `memory_search`, `memory_get`, and the
   dashboard show.

2. **`belief_alpha REAL`, `belief_beta REAL`** (nullable) — a **Beta(α,β) posterior** kept
   *alongside* for ranking experiments only. `mean = α/(α+β)`; corroboration `α += w`,
   contradiction `β += w`. Never shown to the user; only consulted when
   `M3_CONFIDENCE_MODEL=bayesian` (default `transparent`).

### Transparent aggregation formula (the stored `confidence`)

```
confidence = clamp01(
    base_source_conf                      # provenance prior (see table)
  + corroboration_bonus(corroboration_count, distinct_trust_sum)
  - contradiction_penalty(contradiction_count)
)
```

- **`base_source_conf`** — from provenance, reusing fields that already exist:
  | source/origin | prior |
  |---|---|
  | `source='user'` / `change_agent='manual'` | 0.95 |
  | Observer SLM observation (`metadata_json.confidence`) | use the SLM's own 0.6–1.0 |
  | `change_agent` in {claude, gemini, …} (agent-asserted) | `0.70 × agent.trust_score` |
  | `source='internet'` / web_research | 0.40 |
  | unknown | 0.50 (today's `importance` default — neutral) |
- **`corroboration_bonus`** — `min(0.20, 0.05 × Σ distinct-source trust)`. Diminishing; capped so
  three agreeing agents can't exceed a user statement.
- **`contradiction_penalty`** — `min(0.30, 0.10 × contradiction_count)`.
- **`clamp01`** — hard `[0,1]`.

`distinct_trust_sum` and the counts come from a new **`memory_corroborations`** table (Phase 2),
not from re-deriving on every read.

### Trust (Phase 2)

`agents.trust_score REAL DEFAULT 1.0`. Used only as a *multiplier* on agent-asserted priors and a
*weight* in corroboration/contradiction. `1.0` = neutral (so existing agents are unaffected).
Adjustment policy is **explicit and bounded** (no silent drift): trust nudges only via an explicit
`agent_set_trust` tool or a bounded auto-rule (e.g. an agent whose assertions are later
contradicted-and-superseded loses `0.02`, floored at `0.5`). Documented, tested, off by default.

---

# Phases

Each phase: **independently shippable**, **flag-gated**, **green before merge**, **reversible**
(down-migration + flag off). Gate review between phases.

## Phase 0 — Foundations & safety rails (prereq, ~0.5 day)

Build the test/measurement scaffolding *before* touching schema, so every later phase has a net.

- [ ] **Baseline retrieval-quality harness.** A repeatable script that runs a fixed query set
      against a snapshot DB and records ranked IDs + scores, so we can prove "additive &
      backward-compatible" means *byte-identical ranking* when the flag is off. (Reuse the
      `as_of`/search paths; no bench data — synthetic fixtures only.)
- [ ] **Confidence-math module** `bin/memory/confidence.py` — PURE functions
      (`base_source_conf`, `corroboration_bonus`, `contradiction_penalty`, `aggregate`,
      `beta_update`, `beta_mean`). No DB, no I/O. 100% unit-tested first (TDD).
- [ ] **Flag plumbing** in `bin/memory/config.py`:
      `M3_CONFIDENCE_RANKING` (bool, `0`), `M3_CONFIDENCE_WEIGHT` (float, `0.10`),
      `M3_CONFIDENCE_MODEL` (`transparent`|`bayesian`, `transparent`),
      `M3_TRUST_AUTOTUNE` (bool, `0`), `M3_CONSOLIDATION_AUTO` (bool, `0`).
- [ ] **Gate:** confidence.py at 100% branch coverage; harness reproduces current ranking exactly.

## Phase 1 — First-class confidence column (foundation, ~1 day)

- [ ] **Migration `034_confidence.up.sql` / `.down.sql`** (in `memory/migrations/`):
      add `confidence REAL DEFAULT NULL`, `belief_alpha REAL DEFAULT NULL`,
      `belief_beta REAL DEFAULT NULL`, `corroboration_count INTEGER DEFAULT 0`,
      `contradiction_count INTEGER DEFAULT 0`. Index `idx_mi_confidence`. Down drops them.
      **Backfill is lazy/neutral** — `NULL` confidence means "treat as `importance`" so existing
      rows need no UPDATE (follows the 006/009/010 add-column-with-default precedent; avoids a
      71k-row rewrite). One optional follow-up migration can promote
      `metadata_json.confidence` → column for existing observations.
- [ ] **Write path** (`bin/memory/write.py:193→229`): after provenance is resolved, compute
      `confidence` via `confidence.aggregate(...)` using `source`, `change_agent`,
      `metadata_json.confidence` (Observer), and (Phase 2) corroboration. Add `confidence` to the
      INSERT column list + VALUES tuple. New optional param `confidence: float = -1.0`
      (−1 = "derive it"), mirroring `importance`'s convention.
- [ ] **Read path** — surface `confidence` in `memory_get` / `memory_search` result rows
      (display only; no ranking change yet).
- [ ] **Tests:** write→read round-trips confidence; observation's SLM confidence flows to column;
      `NULL`-confidence legacy rows behave exactly as before; down-migration clean.
- [ ] **Gate:** flag still off ⇒ harness ranking byte-identical to Phase 0 baseline.

## Phase 2 — Trust-weighted & consensus provenance (~1.5 days)

- [ ] **Migration `035_trust_and_corroboration`**: `agents.trust_score REAL DEFAULT 1.0`; new
      table `memory_corroborations(id, memory_id, source_kind, source_ref, trust_at_write,
      delta, created_at)` — append-only ledger of who corroborated/contradicted what (mirrors the
      `memory_history` append-only pattern; feeds the aggregation inputs without re-deriving).
- [ ] **Corroboration on write** — when `_check_contradictions` finds a **near-identical** match
      (cosine ≥ a new `CORROBORATION_THRESHOLD` ~0.95 **and** content essentially same — the case
      that today silently makes a 2nd live row), record a `corroborates` row instead, bump the
      existing memory's `corroboration_count`, and re-aggregate its `confidence`. This closes the
      "same fact twice = 2 orphan rows" gap (`write.py:1219-1220`) **without** auto-merging
      distinct content.
- [ ] **Contradiction → confidence** — when a contradiction supersedes, increment the survivor's
      `contradiction_count` and (if `M3_TRUST_AUTOTUNE`) nudge the contradicted agent's trust.
- [ ] **Consensus signal** — `confidence` naturally rises with distinct-source corroboration via
      the ledger; expose `corroboration_count` / `contradiction_count` in results so an agent can
      see "3 sources agree, 1 disagrees."
- [ ] **Tools:** `agent_set_trust(agent_id, trust_score)` (explicit, bounded `[0.5,1.0]`),
      surfaced in catalog; `agent_get` returns `trust_score`.
- [ ] **Tests:** two agents assert same fact → one row, `corroboration_count=2`, higher
      confidence; contradiction lowers it; trust multiplier changes the prior as expected; ledger
      is append-only and replayable.
- [ ] **Gate:** with flag off, retrieval unchanged; ledger writes are additive only.

## Phase 3 — Reinforcement (confidence as a living signal, ~1 day)

- [ ] **Reinforcement in daily maintenance** (`bin/memory_maintenance.py`): a new pass that
      nudges `confidence` from accumulated signal — corroboration raises, contradiction lowers,
      and **age-without-reinforcement gently decays** confidence toward a neutral floor (distinct
      from today's `importance *= 0.995`; confidence decays toward `0.5`, not `0`). Bounded,
      idempotent, logged. Bayesian path (if enabled) updates `belief_alpha/beta` instead.
- [ ] **Access feedback (optional, conservative)** — repeated retrieval is weak positive evidence;
      reuse the existing `access_count` (already tracked, `db.py:362-407`) as a *small* reinforcement
      input, capped hard so "frequently retrieved" can't masquerade as "well-corroborated."
- [ ] **Tests:** reinforcement converges (no oscillation/runaway), respects `[0,1]`, is a no-op
      when no new evidence; decay-toward-neutral never crosses a corroborated floor.
- [ ] **Gate:** maintenance pass is deterministic and reversible (dry-run shows deltas first).

## Phase 4 — Autonomous episodic→semantic consolidation (~1.5 days)

The engine (`memory_consolidate_impl`) exists; this phase gives it a **trigger, a policy, and a
higher-order output type** — and wires it into the existing reflector/queue cadence.

- [ ] **Higher-order type** — add `belief` to `VALID_MEMORY_TYPES` (`mcp_tool_catalog.py:65-83`)
      for *autonomous* consolidations, keeping `summary` for manual/LLM rollups (so the two
      provenance paths stay distinguishable). `belief` rows carry high `confidence` and
      `consolidates` edges to their episodic sources.
- [ ] **Trigger policy** — consume the **existing `reflector_queue`** (fires at
      `M3_REFLECTOR_THRESHOLD=50` observations per user/conversation) as the natural signal: after
      Reflector finishes a group, enqueue a consolidation pass over that group's `observation`
      rows. Sequential dependency made explicit (Reflector → Consolidate), closing the gap the
      enrichment audit flagged.
- [ ] **Background job** `bin/consolidate_beliefs.py` — weekly cron + the queue-drain path; calls
      `memory_consolidate_impl` with `protected_types` honored, `dry_run` first in a "shadow" week.
      Cross-platform install via `install_schedules.py` (new `crontab.template` line +
      launchd/systemd parity).
- [ ] **Guardrails** — never consolidate `preference/user_fact/task/plan` (already protected);
      cap belief count per run; every belief is reversible (soft-delete + `consolidates` edges let
      you reconstruct sources); counterfactual history preserved (originals soft-deleted, not
      purged — matches ChatGPT's "counterfactual memory" ask for free).
- [ ] **Tests:** N observations → 1 belief with correct edges; protected types skipped; dry-run
      writes nothing; re-running is idempotent; belief inherits aggregated confidence.
- [ ] **Gate:** `M3_CONSOLIDATION_AUTO=0` by default; a full shadow run reviewed before enabling.

## Phase 5 — Ranking integration + docs + rollout (~1 day)

- [ ] **PRE-GATE (do first, §11):** build `tests/capture_confidence_baseline.py` and capture the
      flag-OFF byte-fingerprint BEFORE any ranking edit. Every subsequent step keeps this green.
- [ ] **Blend confidence into ranking** behind `M3_CONFIDENCE_RANKING` — additive term
      `M3_CONFIDENCE_WEIGHT × confidence` at both inject points (`search.py:1334` Rust path,
      `:1361` legacy path), exactly like `importance_weight`. `confidence IS NULL` ⇒ fall back to
      `importance` (zero behavior change for un-backfilled rows).
- [ ] **Shadow-mode telemetry** — a debug mode that logs what ranking *would* change without
      changing it, so the deltas are inspectable on real data before promotion.
- [ ] **Docs (the CLAUDE.md regen discipline applies):**
      - New `docs/CONFIDENCE_AND_TRUST.md` (model, formulas, flags, examples).
      - Update `docs/ENVIRONMENT_VARIABLES.md` (5 new flags), `docs/ROADMAP.md` (mark delivered),
        `README.md` knowledge-maintenance section.
      - If any MCP tool added (`agent_set_trust`, etc.): run `gen_tool_manifest.py` +
        `gen_mcp_inventory.py`, update the "N tools" counts, and pass
        `test_tool_count_drift.py` / `test_mcp_catalog_manifest_fresh.py` (mandatory per CLAUDE.md).
- [ ] **Gate:** full suite green; tool-catalog drift check clean; docs regenerated with no drift.

---

# Resilience, hardening, and safety (cross-cutting)

- **Zero-regression invariant.** Every phase keeps a passing assertion that *flag-off ranking is
  byte-identical to the Phase 0 baseline*. This is the contract that makes "additive" real.
- **Reversibility.** Every migration ships a tested `.down.sql`; every behavior is flag-gated off
  by default; consolidation soft-deletes (never purges) so beliefs can be unwound.
- **Bounded math.** All confidence/trust updates are clamped `[0,1]` / `[0.5,1.0]`, diminishing,
  and capped so no single signal (frequency, one loud agent) can dominate. No unbounded feedback.
- **Idempotency.** Maintenance/consolidation passes are safe to re-run; `dry_run` precedes every
  destructive-ish pass; ledgers are append-only and replayable.
- **Performance.** No per-read re-derivation — confidence is a stored column; corroboration counts
  come from the ledger. The 71k-row table is not rewritten (lazy NULL backfill). Daily passes are
  batched like existing maintenance and VACUUM-aware.
- **Crypto/integrity reuse.** `content_hash` + the existing `audit_trail.py` hash-chain already
  cover tamper-evidence; corroboration/contradiction events are auditable through `memory_history`
  + the new ledger. No new crypto surface.
- **Migration discipline.** Numbers are append-only from 034; both `memory/migrations/` entries get
  up+down; `schema_versions` tracks them; test on a *copy* of the engine DB first.
- **Bench/PII hygiene.** Tests use synthetic fixtures only — no LME/LongMemEval data. Pre-push
  bench-leak + remote-visibility audit per CLAUDE.md before anything reaches a public remote.

# Risk register

| Risk | Mitigation |
|---|---|
| Confidence ranking silently changes retrieval | Flag default off + byte-identical baseline gate + shadow telemetry before promote |
| 71k-row migration is slow/locks DB | Lazy NULL backfill (no row rewrite); add-column-with-default precedent |
| Corroboration merges genuinely-distinct facts | High `CORROBORATION_THRESHOLD` (~0.95) + content-identity check; only the "exact re-write" case, never distinct content |
| Trust autotune drifts / punishes good agents | `M3_TRUST_AUTOTUNE=0` default; bounded `[0.5,1.0]`; explicit `agent_set_trust`; every nudge logged |
| Autonomous consolidation destroys nuance | Protected types; dry-run shadow week; soft-delete + edges = reversible; per-run cap |
| Reinforcement oscillates/runs away | Bounded, diminishing, idempotent; decay-toward-neutral floor; convergence test |
| Docs/“N tools” drift on new tools | Mandatory regen + drift tests in Phase 5 gate (CLAUDE.md) |

# Sequencing & estimate

```
Phase 0 (rails)  →  Phase 1 (confidence col)  →  Phase 2 (trust+corroboration)
                                                      ↓
Phase 5 (ranking+docs)  ←  Phase 4 (consolidation)  ←  Phase 3 (reinforcement)
```
Rough: ~7–8 focused days. Phases 1–2 are the high-leverage core; 3–4 deliver the "knowledge
steward" capability; 5 makes it visible. **Recommended commit:** build Phase 0+1 now, gate-review,
then proceed — per the "plan all 4, phase the build" spirit even though scope is all 4.

# Resolved decisions (2026-06-27)

1. **Belief type name** → **new `belief`**. Keeps autonomous consolidations distinguishable from
   manual/LLM `summary` rollups (distinct provenance paths stay legible).
2. **Trust autotune** → **explicit `agent_set_trust` only** in Phase 2; the bounded auto-rule is
   deferred (no silent trust drift until we've watched explicit trust behave).
3. **Backfill** → **stay lazy** (NULL `confidence` ⇒ treat as `importance`). Phase 1 populates new
   writes only; an *optional* follow-up migration can promote existing
   `metadata_json.confidence` → column once the column has proven out.
```
