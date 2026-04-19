# LoCoMo Phase1 Retrieval Improvement Plan

**Branch:** `main` (this plan lives on main alongside the drivers it targets)
**Published baseline (500q, audit_20260417_141947):** any_gold_hit_rate 86.0%, mean_r@10 17.3%
**Best-measured variant (VARIANT_REPORT_5x500):** `llm_v1` — mean_r@10 30.5% (+13.2pp); `llm_v1_title_ctx` adds only noise-band gains at extra LLM cost, so **this plan targets `llm_v1`**.

## 1. Framing

LoCoMo Phase1 is a **retrieval-only audit** — we measure whether the gold evidence turn is in the top-k retrieved pool, not whether the downstream answerer uses it correctly. Two metrics matter:

- `any_gold_hit_rate`: fraction of questions where at least one gold `dia_id` appears anywhere in the retrieved pool (size k=40). Already 86% at baseline — the pool usually contains the answer.
- `mean_r@k`: fraction of gold evidence turns that rank within the top-k. **This is the bottleneck.** Baseline mean_r@10 is 17.3%.

### Per-category gap (500q baseline, audit_20260417_141947)

| Category | n | any_gold_hit | mean_r@10 | mean_r@40 |
|---|---|---|---|---|
| single-hop | 200 | 83.5% | 17.3% | 37.0% |
| multi-hop | 75 | 90.7% | **6.0%** | 12.1% |
| temporal | 91 | 82.4% | 11.0% | 22.5% |
| adversarial | 112 | 90.2% | 32.6% | 46.9% |
| open-domain | 22 | 86.4% | 4.5% | 22.7% |
| **Overall** | **500** | **86.0%** | **17.3%** | **32.2%** |

**Where to invest:**

- **multi-hop mean_r@10 = 6.0%** — needle-in-haystack cross-session. Primary target.
- **temporal mean_r@10 = 11.0%** — date binding weakness. Already partially addressed by `smart_time_boost` + `extract_referenced_dates` on main; needs measurement.
- **open-domain n=22** — tiny sample; don't optimize against.
- **adversarial 32.6%** — already strong; don't regress it.

### What VARIANT_REPORT_5x500 showed

| Variant | Overall r@10 | Temporal r@10 | Multi-hop r@10 | Single-hop r@10 |
|---|---|---|---|---|
| baseline | 17.3% | 11.0% | 6.0% | 17.3% |
| heuristic_c1c4 | 30.9% | 23.1% | 10.3% | 39.5% |
| **llm_v1** | **30.5%** | 18.7% | 9.9% | 38.0% |
| llm_only | 27.9% | 17.6% | 8.4% | 34.5% |
| llm_v1_title_ctx | 31.2% | 23.1% | 8.6% | 37.5% |

Conclusions:
1. **Any form of key-expansion (heuristic or LLM) lifts mean_r@10 by ~10-13pp.** Raw-only baseline is dominated.
2. **`llm_v1` is the target.** `llm_v1_title_ctx` adds only +0.7pp overall, regresses multi-hop by 1.3pp vs `llm_v1`, and costs extra prompt tokens (session-gist + entity-hint context fed into every LLM call).
3. `llm_only` (keys replaced entirely by LLM summary, losing raw text) regresses — matches LongMemEval paper finding that fact-only values regress single-session categories.
4. Multi-hop improves modestly across variants (6.0→10.3%) but stays the weakest category; key-expansion alone doesn't solve needle-in-haystack.

## 2. Code placement contract

| Kind of change | Goes in | Visibility |
|---|---|---|
| LoCoMo-specific ingest (C1-C4 heuristics, session anchoring, dia_id mapping) | `bin/bench_locomo.py` | benchmark-only |
| Shared ingest/retrieval levers (contextual key-expansion, smart-time-boost, neighbor-session, rerank) | `bin/memory_core.py` / `bin/temporal_utils.py` with gated flags default-off | shared, both benches |
| Dataset anchor-date parser (LOCOMO's `"1:56 pm on 8 May, 2023"`) | `bin/bench_locomo.py`, registered via `temporal_utils.register_anchor_parser()` | benchmark-only |

## 3. Current state on main

**Shared machinery (already on main, gated, default-off):**

- `memory_core.memory_search_scored_impl(smart_time_boost=0.0, smart_neighbor_sessions=0, recency_bias=0.0)` — numeric kwargs on the shared scorer
- `temporal_utils.extract_referenced_dates` / `has_temporal_cues` — time-aware helpers
- `temporal_utils._ANCHOR_PARSERS` registry + `parse_anchor_date()` — each bench registers its own format parser
- `memory_core._memory_search_gated_validator` — hides bench variant rows from default MCP searches

**Phase1 artifacts on main:**

- `benchmarks/Phase1/retrieval_audit.py` — driver
- `benchmarks/Phase1/reingest.py` — variant re-ingest helper
- `benchmarks/Phase1/compare_runs.py` — pairwise comparison (requires `--a` / `--b`)
- `benchmarks/Phase1/join_variant_reports.py` — aggregates variant runs into a report
- `benchmarks/Phase1/probe_ingest_cost.py` — ingest cost probe
- `benchmarks/Phase1/stamp_variants_from_chainlog.py` — stamp variant tags onto chain log rows
- `benchmarks/Phase1/VARIANT_REPORT_4x500.md`, `VARIANT_REPORT_5x500.md` — shipped variant reports
- `benchmarks/Phase1/runs/audit_20260417_*/` — per-run artifacts; 5 of the 5-variant runs have summary.json

**Runs WITHOUT summary.json on main** (baseline + post-port runs have only `retrieval_trace.jsonl`):
- `audit_20260417_141947` (the 500q baseline referenced by this plan)
- `audit_20260417_163654`, `audit_20260417_165238`
- `baseline_pre_port`, `baseline_preport_clean`, `post_port_plus_fixes`

These need summary regeneration (can be done from `retrieval_trace.jsonl` alone) to make the baseline usable for regression comparison.

**NOT on main (lives on `bench-Phase1`):**

- `llm_v1_title_ctx` prompt changes in `memory_core.py` — superseded by `llm_v1` as plan target
- `_extractive_title`, `_merge_short_turns`, `_session_gist`, `_session_context_for` in `bin/bench_locomo.py` — C1-C4 heuristic stack
- `analyze_handoff.py`, `analyze_prompt.py`, `probe_issues.py` — Phase 2/3 analysis tools
- `VARIANT_PRESETS` with `llm_v1_title_ctx` entry — not needed under this plan

## 4. Regression-detection contract

Every retrieval change must produce a Phase1 retrieval audit on the canonical 500q before merging to `main`. Gates:

1. **Baseline** is the most recent measured variant on main — currently `llm_v1` once ported, or `audit_20260417_141947/summary.json` (once regenerated) for raw-only comparison.
2. **Run** `python benchmarks/Phase1/retrieval_audit.py` on the canonical subset: 500 questions spanning conv-26, conv-30, conv-41, conv-42.
3. **Compare** with `python benchmarks/Phase1/compare_runs.py --a <baseline> --b <new>`. A change is blocked if:
   - Any category's `any_gold_hit_rate` drops — mechanical regression (pool coverage shrunk).
   - Any category's `mean_r@10` drops ≥1.5pp (provisional noise band; Phase1 re-runs are retrieval-deterministic so differences should be near-zero).
   - `zero_hit_count` increases by ≥3.
4. **Noise**: Phase1 runs are retrieval-deterministic given the same DB and seed. Any numerical difference under identical config is a bug to investigate.
5. **Cross-benchmark gate**: any change that modifies `memory_core.py` or `temporal_utils.py` must also pass the LongMemEval smart-retrieval 500q audit (see `benchmarks/longmemeval/PLAN.md`) before merging.

## 5. Staged plan

### Stage 0 — Regenerate baseline summary.json

**Problem:** the canonical 500q baseline (`audit_20260417_141947`) only has `retrieval_trace.jsonl` on main. `compare_runs.py` needs `summary.json`.

**Actions:**
1. Port (or write) a small `summarize_trace.py` helper that reads `retrieval_trace.jsonl` and emits `summary.json` with the per-category metrics described in Section 1.
2. Run it against `audit_20260417_141947`, `baseline_preport_clean`, `post_port_plus_fixes`.
3. Commit the regenerated summary files.

**Cost:** <1hr coding + regeneration.

**Exit:** baseline + post-port-state summary.jsons present on main for diff comparison.

### Stage 1 — Productionize the `llm_v1` key-expansion

**Hypothesis:** +13.2pp mean_r@10 on LoCoMo (from VARIANT_REPORT_5x500). `llm_v1` uses heuristic title + LLM auto-title + LLM auto-entities on the embed key, not `llm_v1_title_ctx`'s additional session-context machinery (which doesn't earn its LLM cost).

**Code placement:**
- **Shared (gated):** Add `embed_key_enricher: Callable[[dict], str] | None = None` kwarg to `memory_write_bulk_impl` in `bin/memory_core.py`. Default `None` preserves current behavior exactly. When supplied, the enricher receives the raw item dict and returns the string to embed (value still stored verbatim). No category coupling.
- **Benchmark:** `bin/bench_locomo.py` provides a LoCoMo-specific enricher that computes `heuristic_title | llm_title | llm_entities | raw_turn` and passes it in. The enricher uses `_maybe_auto_title` / `_maybe_auto_entities` already in `memory_core.py`.
- **CLI:** `bin/bench_locomo.py` adds `--contextual-keys` flag that wires the enricher.

**Approach:**
1. Implement the `embed_key_enricher` hook in `memory_core.py` with unit test (enricher called once per item, output embedded, value stored raw).
2. Implement LoCoMo enricher in `bin/bench_locomo.py`. Re-use the existing `_maybe_auto_title` / `_maybe_auto_entities` paths from memory_core.
3. Add `--contextual-keys` flag; re-ingest LoCoMo under variant `locomo-llm_v1-<date>`.
4. Run full 500q Phase1 audit. Confirm `mean_r@10 ≈ 30.5%` (the published `llm_v1` number). Delta band: within ±2pp.
5. If reproduction passes, document the new baseline on main.

**Cost:** code + re-ingest (~30min) + audit (~10min). LLM cost from auto-title/auto-entities is the dominant item.

**Exit:** key-expansion is in production code as a gated hook, measured to preserve the Phase1 `llm_v1` win. Both benches can now opt in.

### Stage 2 — Measure smart-retrieval on LoCoMo (already on main, untested here)

**Problem:** `smart_time_boost` + `smart_neighbor_sessions` + `extract_referenced_dates` shipped to main in commit `2235f88` but were never measured on LoCoMo directly. The LME-side published the temporal lift; LoCoMo should benefit similarly on the temporal category (baseline 11.0% mean_r@10).

**Code placement:** no new code. `bin/bench_locomo.py` already has `--enable-smart-retrieval` via the driver's flag.

**Approach:**
1. Run Phase1 audit WITHOUT smart-retrieval: `python benchmarks/Phase1/retrieval_audit.py --limit 500`. Save as `audit_stage2_baseline_<ts>`.
2. Run Phase1 audit WITH smart-retrieval: `... --enable-smart-retrieval`. Save as `audit_stage2_smart_<ts>`.
3. `compare_runs.py` both. Target: temporal mean_r@10 lift; overall ± noise.
4. Decision: if temporal lifts ≥3pp and no category regresses ≥1.5pp, smart-retrieval becomes default-on for Phase1 audits going forward.

**Cost:** two audit runs, retrieval-only (~10min each).

**Exit:** quantified LoCoMo benefit of the existing smart-retrieval machinery; `bin/bench_locomo.py` default updated if warranted.

### Stage 3 — Multi-hop targeted retrieval

**Hypothesis:** multi-hop questions stay at 6-10% mean_r@10 even with `llm_v1`. Cross-session connective tissue isn't retrievable by embedding similarity alone. Candidate levers:

- **Neighbor-session expansion** (`smart_neighbor_sessions > 0`) — already on main. Stage 2 will measure its effect on multi-hop; if not enough, tune.
- **Cross-encoder rerank over expanded pool** — Stage 2 of the LongMemEval PLAN ports rerank as a shared primitive (`bin/rerank_utils.py`). Consume here once available. Add `--rerank` flag to `bin/bench_locomo.py`.

**Code placement:** rerank primitive is shared; LoCoMo-side flag plumbing only.

**Approach:**
1. Gate on LongMemEval PLAN Stage 2 shipping `bin/rerank_utils.py`.
2. Add `--rerank --rerank-pool-k <N>` to bench_locomo.
3. Run audit across three configs: baseline, `llm_v1`, `llm_v1 + rerank`. Pick winner on multi-hop mean_r@10.

**Cost:** ~1 day (once rerank_utils lands).

**Exit:** multi-hop mean_r@10 lifts ≥5pp above Stage 1 baseline, or clean rejection.

### Stage 4 — Expand beyond 500q

Current audit is 500q of 1974 available. After Stages 0-3:

1. Run audit on full 1974q to confirm trends hold.
2. Only after full-audit confirms: cascade to the LoCoMo QA pipeline (`bin/bench_locomo.py` end-to-end, not retrieval-only).
3. Phase 2 + Phase 3 (prompt analysis + judge verification) are separate initiatives.

## 6. Test procedures (how-to)

### A — Phase1 retrieval audit (canonical 500q)

```bash
python benchmarks/Phase1/retrieval_audit.py \
  --dataset data/locomo/locomo10.json \
  --limit 500 --k 40 \
  --variant "<descriptive-tag>" \
  2>&1 | tee .scratch/locomo_audit_$(date +%Y%m%d_%H%M%S).log
```

Artifacts end up in `benchmarks/Phase1/runs/audit_<ts>/`:

- `retrieval_trace.jsonl` — one line per question: gold dia_ids, retrieved dia_ids, hit positions.
- `summary.json` — aggregate per-category metrics (schema shown in Section 1).
- `zero_hit_questions.json` — questions where no gold dia_id appeared.
- `run.log` — progress.

### B — Variant comparison

```bash
python benchmarks/Phase1/compare_runs.py \
  --a audit_20260417_141947 \
  --b audit_<new_ts>
```

Output: overall + per-category delta table. Use as regression check.

### C — Re-ingest under a new variant

```bash
python benchmarks/Phase1/reingest.py \
  --samples conv-26 conv-30 conv-41 conv-42 \
  --variants llm_v1
```

Always re-ingest fresh under a new `change_agent` so cleanup is targeted and old variants don't contaminate future audits. Variant presets live in `benchmarks/Phase1/reingest.py::VARIANT_PRESETS`.

### D — Zero-hit debugging loop

For any category where `zero_hit_count` > 0:

1. Inspect each zero-hit question against the conversation (`data/locomo/locomo10.json`).
2. Classify: wrong-session retrieved, right-session but wrong turn, or gold turn not indexed.
3. Feed back into Stage 2/3 design if a systemic failure mode emerges.

### E — Quick smoke (200q)

```bash
python benchmarks/Phase1/retrieval_audit.py \
  --dataset data/locomo/locomo10.json \
  --limit 200 --k 40 --variant "smoke-<feature>"
```

Wall time: ~5-10 min.

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| LoCoMo label quality (29.6% judge floor, wrong-date contradictions) contaminates retrieval signal | Stay retrieval-only until Stage 4; judge issues are downstream of pool quality |
| Key-expansion prompt drift across variants | Pin the exact prompt text (use the existing `_maybe_auto_title`/`_maybe_auto_entities` system prompts on main); record the prompt hash in each variant's `summary.json` |
| Temporal date extraction on LoCoMo turns misparses (short turns, informal dates) | Unit-test `extract_referenced_dates` against a fixture of LoCoMo turn samples before enabling on full ingest |
| Multi-hop stays stuck regardless of retrieval changes | Expected — may require answer-side reasoning changes (chain-of-thought, timeline construction). Scope outside this plan |
| Dataset subset (500/1974) biases conclusions | Stage 4 full-1974 audit before declaring victory |
| Shared `memory_core.py` changes break LongMemEval | Cross-benchmark gate: any main-side change requires both LoCoMo Phase1 + LongMemEval smart-retrieval audits pass |
| Port of `llm_v1` pulls in `llm_v1_title_ctx` prompt surgery by mistake | Plan target is `llm_v1` only; use existing `_maybe_auto_title` / `_maybe_auto_entities` on main — do not touch their prompts |

## 8. Timeline

| Stage | Wall time | Gating |
|---|---|---|
| 0 — regenerate summaries | 0.5-1hr | blocks comparison tooling |
| 1 — productionize llm_v1 | 1 day | blocks Stage 3 |
| 2 — measure smart-retrieval on LoCoMo | 0.5 day | independent |
| 3 — multi-hop levers | 1 day | blocked on LongMemEval PLAN Stage 2 (rerank_utils) |
| 4 — full-1974 expansion | 0.5 day | after Stage 1-3 |

## 9. References

- On-branch baseline: `benchmarks/Phase1/runs/audit_20260417_141947/` (summary.json pending regeneration)
- Companion: [`benchmarks/longmemeval/PLAN.md`](../longmemeval/PLAN.md)
- Variant report: [`benchmarks/Phase1/VARIANT_REPORT_5x500.md`](./VARIANT_REPORT_5x500.md)
- Paper: [LoCoMo ACL 2024](https://aclanthology.org/2024.acl-long.747.pdf) — dia_id + session timestamps; 9K token, 35-session dialogues
- LongMemEval paper (relevant for cross-benchmark transfer): [ICLR 2025](https://arxiv.org/html/2410.10813v1)
