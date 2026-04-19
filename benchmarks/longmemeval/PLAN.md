# LongMemEval-S Retrieval Improvement Plan

**Branch:** `main` (this plan lives on main alongside the harness it targets)
**Published baseline:** 89.0% stock / 74.8% smart-retrieval / 68.0% no-knobs (500q, Opus 4.6 answerer, gpt-4o judge)
**Real-world target:** smart-retrieval path (no oracle metadata), currently 74.8%

## 1. Framing

The headline 89.0% uses the dataset's `question_type` field as privileged input to select per-category retrieval policies. That field does not exist at runtime for a real agent. This plan does **not** try to push the 89.0% number further. It pushes the 74.8% **smart-retrieval** path toward the 89.0% ceiling by adding capabilities that replace the oracle with runtime-inferable signals.

### Performance layers (500q, published)

| Layer | Overall | ss-user | ss-assistant | ss-preference | multi-session | temporal | knowledge-update |
|---|---|---|---|---|---|---|---|
| Stock (oracle) | 89.0 | 91.4 | 94.6 | 93.3 | 85.0 | 86.5 | 92.3 |
| Smart-retrieval (no oracle) | 74.8 | — | — | — | — | — | — |
| Adaptive-k only | 72.6 | — | — | — | — | — | — |
| No category knobs | 68.0 | 82.9 | 75.0 | 73.3 | 57.1 | 57.1 | 84.6 |
| RAG-aware empty | 8.4 | 8.6 | 19.6 | 0.0 | 9.0 | 5.3 | 7.7 |
| Neutral prompt | 6.4 | 8.6 | 0.0 | 3.3 | 8.3 | 6.0 | 7.7 |

**Gap analysis:** 14.2pp from smart-retrieval to stock. The `--no-category-knobs` deltas show where the oracle contributes most — multi-session −27.9pp, temporal −29.4pp, ss-preference −20.0pp, ss-assistant −19.6pp. These four categories are where smart-retrieval needs to recover.

### Non-goals

- Do not push the oracle 89.0% number. It is already published and not the realistic ceiling.
- Do not rely on strat60 smoke runs for publish-grade decisions — they are retrieval-quality checks only.
- Do not add benchmark-specific category names (ss-user, ss-assistant, etc.) into `memory_core.py`. Category-aware logic that depends on LongMemEval labels stays in `bin/bench_longmemeval.py`.

## 2. Code placement contract

This plan adheres to the following split:

| Kind of change | Goes in | Visibility |
|---|---|---|
| LongMemEval-specific prompts, dataset munging, category knobs keyed to LME labels | `bin/bench_longmemeval.py` | benchmark-only |
| Shared retrieval/ingest levers (rerank, HyDE, key-expansion, time boost) | `bin/memory_core.py` / `bin/temporal_utils.py` with gated flags default-off | shared, both benches |
| Dataset anchor-date parser (LME's `"2023/05/20 (Sat) 02:21"`) | `bin/bench_longmemeval.py`, registered via `temporal_utils.register_anchor_parser()` | benchmark-only |

## 3. Current state on main

**Shared machinery (already on main, gated, default-off):**

- `temporal_utils.extract_referenced_dates` / `has_temporal_cues` — time-aware helpers
- `memory_core.memory_search_scored_impl(smart_time_boost=0.0, smart_neighbor_sessions=0, recency_bias=0.0)` — kwargs on the shared scorer, all numeric, no category-string leakage
- `memory_core._memory_search_gated_validator` — hides bench variant rows from default MCP searches (`include_bench_data=False` default)
- `temporal_utils._ANCHOR_PARSERS` registry — each bench registers its own anchor-date parser

**Bench harness on main (`bin/bench_longmemeval.py`, 867 lines):**

- `--smart-retrieval` (published 74.8%) — wires smart_time_boost + smart_neighbor_sessions
- `--adaptive-k` (published 72.6%)
- `parse_longmemeval_date()` lives here, auto-registers with `temporal_utils`

**NOT on main (lives on `bench-LME` or `backup-bench-wip-pre-merge` backup branch):**

- `--rerank` / `_RERANKER` / `_RERANKER_NAME` / `--rerank-pool-k` — cross-encoder rerank over retrieved pool
- `--hyde` / `_hyde_expand` — HyDE query expansion
- `--reflection` — reflection-mode answer prompt
- `--chain-of-note` — chain-of-note extraction
- `--ingest-mode {turn,session}` — multi-mode ingest
- `--no-category-knobs` — ablation flag
- `RECENCY_BIAS_CATEGORIES` — per-category recency boost (LongMemEval-specific, belongs only in bench_longmemeval.py)
- `role_boost` (ss-user / ss-assistant) — LongMemEval-specific category-name boost, belongs only in bench_longmemeval.py
- `BENCH_RUN_ID` / `BENCH_CHANGE_AGENT` / `--wipe-run` / `--wipe-all-bench` — provenance + targeted wipe helpers

## 4. Regression-detection contract

Every retrieval change must produce a judged full-500q run before merging to `main`. Gates:

1. **Baseline** is the most recent captured smart-retrieval `results.json` on the target dataset.
2. **Run** the full 500q with `--smart-retrieval --skip-ingest` against the existing DB (reuses ingest; eliminates ingest variance as a confound).
3. **Compare** per-category `accuracy` deltas. A change is blocked if:
   - Any category drops ≥1.5pp (the shipped reproducibility band).
   - `retrieval_session_hit_rate` drops by any amount (mechanical regression).
   - `overall_accuracy` drops ≥1.5pp.
4. **Noise band**: differences under ±1.5pp are treated as noise (published data shows ≈±0.7pp single-direction variance).
5. **Budget**: plan for 2 runs per trialed feature — one A, one A+feature — on the same DB snapshot, same day, same LM Studio / llama-server process.
6. **Cross-benchmark gate**: any change that modifies `memory_core.py` or `temporal_utils.py` must also pass the Phase1 LoCoMo audit (see `benchmarks/Phase1/PLAN.md`) before merging.

## 5. Test inventory

### Existing harness bits (main)

- `bin/bench_longmemeval.py` — entry point. Writes `results.json` + `hypotheses.jsonl` + `run.log` to `.scratch/longmemeval_run_<ts>/`.
- `bin/rejudge_*.py` — re-scores an existing hypothesis file under a new judge config (use when judge changes, not when retrieval changes).
- `data/longmemeval/longmemeval_s_cleaned.json` — 500 canonical questions.
- `data/longmemeval/longmemeval_s_strat60.json` — 60-question stratified sample (retrieval smoke only, do not publish).

### Existing flags (main)

| Flag | Status |
|---|---|
| `--smart-retrieval` | shipped, published 74.8% |
| `--adaptive-k` | shipped, published 72.6% |

### New flags to add (staged)

| Flag | Stage | Code placement | Purpose |
|---|---|---|---|
| `--runtime-category-classifier {off,regex,slm}` | 1 | `bin/bench_longmemeval.py` | replace oracle `question_type` with runtime signal (LME-specific labels) |
| `--rerank` / `--rerank-pool-k` | 2 | shared primitive in new `bin/rerank_utils.py`, opted in by LME flag | cross-encoder rerank over top-N pool |
| `--hyde` | 3 | shared primitive in new `bin/hyde_utils.py`, opted in by LME flag | HyDE query expansion |
| `--contextual-keys` | 4 | gated hook in `memory_core.py` (see Phase1 PLAN) | consume llm_v1 key-expansion if it lands via LoCoMo plan |
| `--ingest-mode {turn,session}` | optional | `bin/bench_longmemeval.py` | LME-only ablation control |
| `--no-category-knobs` | optional | `bin/bench_longmemeval.py` | LME-only ablation control |

### New test artifacts

| Artifact | Location | When written |
|---|---|---|
| `benchmarks/longmemeval/smart_baseline.json` | main | Stage 0 — captured smart-retrieval per-category baseline |
| `benchmarks/longmemeval/stage1_runtime_classifier.json` | main | Stage 1 — after classifier |
| `benchmarks/longmemeval/stage2_rerank.json` | main | Stage 2 — after rerank |
| `benchmarks/longmemeval/stage3_hyde.json` | main | Stage 3 — after HyDE trial |
| `benchmarks/longmemeval/stage4_contextual_keys.json` | main | Stage 4 — after LoCoMo key-expansion lands |

## 6. Staged plan

### Stage 0 — Capture smart-retrieval per-category baseline (blocking)

**Problem:** the shipped `results.json` only has stock 89.0%. Per-category accuracy for the smart-retrieval path (74.8%) was never saved as a comparable artifact. Without it, every future change is optimizing blind.

**Actions:**
1. Confirm DB has the 500q ingest tagged `change_agent='bench:lme-20260415-174852-*'` (see shipped README).
2. `python bin/bench_longmemeval.py --smart-retrieval --skip-ingest --dataset data/longmemeval/longmemeval_s_cleaned.json --limit 500 --k 10 --answer-model claude-opus-4-6 --judge-model gpt-4o 2>&1 | tee .scratch/lme_smart_baseline.log`
3. Copy `.scratch/longmemeval_run_*/results.json` → `benchmarks/longmemeval/smart_baseline.json`.
4. Document absolute per-category numbers in this file (update Section 1 table).

**Cost:** ~2hr wall (retrieval + Opus answer + gpt-4o judge on 500q, no ingest).

**Exit:** per-category smart-retrieval breakdown committed to repo; all downstream stages reference it.

### Stage 1 — Runtime question-signal inference

**Hypothesis:** most of the 14.2pp gap comes from category-specific `k` selection and retrieval-weight tuning the oracle provides. A runtime classifier that infers category from the question alone should recover a material fraction without leaking benchmark labels at inference time.

**Code placement:** entire classifier lives in `bin/bench_longmemeval.py` because it uses LongMemEval's 6 category names as output labels. `memory_core.py` never sees category strings.

**Approach:**
1. Add `_classify_question(query: str) -> str` in `bin/bench_longmemeval.py` (pure-regex classifier):
   - `temporal`: date tokens, "when", "how long ago", "before/after", month names.
   - `preference`: "prefer", "favorite", "like/dislike", "rather".
   - `multi-session`: conjunctions across time ("and also", "compared to", "over time").
   - `ss-assistant`: questions about what *the assistant* said/suggested/recommended.
   - Default: `ss-user`.
2. Add `--runtime-category-classifier {off,regex,slm}` flag. When `off`, smart-retrieval behaves as published. When `regex`, the classifier output is substituted for the oracle `question_type` at retrieval time.
3. **Training/validation discipline:** hand-write rules against a **held-out 50q sample** from LongMemEval-M (the long variant, not -S). Do not look at the -S 500q when writing rules. This avoids the oracle leak in disguise.
4. Validate classifier agreement with oracle on held-out set (target ≥80% agreement).
5. Run full 500q smart-retrieval with classifier `on`. Record `stage1_runtime_classifier.json`.
6. If overall accuracy lifts ≥3pp over Stage 0 baseline and no category regresses ≥1.5pp: retain on main, default `off`.

**Stretch:** if regex plateau is low, add `--runtime-category-classifier slm` path using a local small model (Qwen 1.5B via llama-server) as zero-shot classifier. Only pursue if regex closes <5pp.

**Exit:** either measured lift ≥3pp on smart-retrieval path, or clean rejection with the reason captured in the results JSON.

### Stage 2 — Cross-encoder rerank

**Hypothesis:** Anthropic's contextual-retrieval writeup reports +13pp from cross-encoder rerank on top of contextual embeddings. The mechanism (rescore the top-N pool with a discriminative model) is benchmark-agnostic.

**Code placement:**
- **Shared:** new `bin/rerank_utils.py` provides `async rerank_pool(query: str, candidates: list[dict], pool_k: int, model: str) -> list[dict]`. No category coupling, no benchmark awareness. Default model is a cross-encoder available locally via llama-server.
- **Benchmark:** `bin/bench_longmemeval.py` gains `--rerank` + `--rerank-pool-k <N>` flags; when set, the harness calls `rerank_utils.rerank_pool` after the base retrieval.

**Approach:**
1. Implement `bin/rerank_utils.py`. Unit test with a fixed candidate set.
2. Wire `--rerank --rerank-pool-k 40` into bench_longmemeval.
3. Full 500q judged run with rerank on top of smart-retrieval. Record `stage2_rerank.json`.
4. Regression gate from Section 4.

**Exit:** rerank retained if overall lift ≥2pp and no category regresses ≥1.5pp.

### Stage 3 — HyDE query expansion (trial-only)

**Hypothesis:** HyDE (hypothetical-document embedding) lifts recall on questions where the query text is lexically distant from the evidence. Paper warns weak LLMs hallucinate time cues, so this is a trial, not a guaranteed win.

**Code placement:**
- **Shared:** new `bin/hyde_utils.py` provides `async hyde_expand(query: str, model: str) -> str`. Pure function, no benchmark coupling.
- **Benchmark:** `bin/bench_longmemeval.py` gains `--hyde` flag; when set, the harness calls `hyde_utils.hyde_expand` and uses the expanded text as the retrieval query.

**Approach:**
1. Implement `bin/hyde_utils.py`.
2. Wire `--hyde` into bench_longmemeval.
3. Full 500q judged run. Record `stage3_hyde.json`.
4. Regression gate: **temporal category** is the critical check — HyDE commonly regresses time-sensitive queries.

**Exit:** retained only if overall ≥2pp lift AND temporal ≥baseline. Otherwise drop.

### Stage 4 — Consume LoCoMo key-expansion (`llm_v1`) win if it lands

**Dependency:** Phase1 PLAN Stage 1 ports `llm_v1` (LLM auto-title + auto-entities enrichment on embed key) to `memory_core.py` as a gated hook (`contextual_keys=False` default kwarg on `memory_write_bulk_impl`). If that lands and proves its LoCoMo number, trial it here.

**Code placement:** the hook is already in shared code (from Phase1 work); bench_longmemeval just adds `--contextual-keys` flag that flips the kwarg. No LME-specific logic.

**Approach:**
1. Gate on Phase1 Stage 1 completion. If not landed, defer this stage.
2. Re-ingest LongMemEval-S with `--contextual-keys` under a new variant tag (`lme-contextual-500`).
3. Full 500q judged run. Record `stage4_contextual_keys.json`.
4. **Paper discipline** (applies here): expand keys, not values. Values stay verbatim.

**Exit:** retained if overall ≥2pp lift AND ss-* categories within ±1.5pp (paper warns value-level expansion regresses single-session; key-only should avoid this).

### Stage 5 — Publish refresh

Once Stages 0-4 produce a measurable smart-retrieval lift, update `benchmarks/longmemeval/README.md`:

- Add smart-retrieval per-category table (closes the audit gap).
- Reframe 74.8% → new number as the realistic ceiling.
- Keep 89.0% as the oracle-metadata upper bound.
- Add the new ablation rows (runtime classifier, rerank, etc.).
- Update the "14.2pp gap" paragraph to reflect what closed and what remains.

## 7. Test procedures (how-to)

### A — Smart-retrieval per-category run

```bash
python bin/bench_longmemeval.py \
  --smart-retrieval --skip-ingest \
  --dataset data/longmemeval/longmemeval_s_cleaned.json --limit 500 \
  --k 10 --k-reasoning 20 \
  --answer-model claude-opus-4-6 --judge-model gpt-4o \
  2>&1 | tee .scratch/lme_smart_$(date +%Y%m%d_%H%M%S).log
```

Artifacts end up in `.scratch/longmemeval_run_<ts>/results.json`. Copy to `benchmarks/longmemeval/` under a descriptive name for each stage.

### B — Strat60 retrieval smoke (pre-flight only)

```bash
PYTHONIOENCODING=utf-8 EVAL_GENERATOR_MODEL=stub python -m bin.bench_longmemeval \
  --dataset data/longmemeval/longmemeval_s_strat60.json \
  --limit 60 --skip-ingest --no-judge --k 10 \
  --smart-retrieval 2>&1 | tail -40
```

No judge, no answerer — only measures session_hit_rate. Do not publish from this.

### C — Per-category delta diff

```bash
python - <<'PY'
import json
a = json.load(open('benchmarks/longmemeval/smart_baseline.json'))
b = json.load(open('benchmarks/longmemeval/stage1_runtime_classifier.json'))
print(f"overall  {a['overall_accuracy']:.3f} -> {b['overall_accuracy']:.3f}  Δ{(b['overall_accuracy']-a['overall_accuracy'])*100:+.1f}pp")
for cat in a['per_type']:
    ai = a['per_type'][cat]['accuracy']; bi = b['per_type'][cat]['accuracy']
    print(f"  {cat:28}  {ai:.3f} -> {bi:.3f}  Δ{(bi-ai)*100:+.1f}pp")
PY
```

### D — Regression alarm

Any run that violates the Section 4 contract:

1. Save the offending `results.json` as `regressions/<branch>-<ts>.json`.
2. Open an issue or log under `benchmarks/longmemeval/regressions.md`.
3. Do not merge until root cause is identified or the feature is reverted.

### E — Reproducibility verification

Before drawing any conclusion from a sub-2pp delta, re-run the same config once more. Published band is ±0.7pp; confirm the delta sign holds across both runs.

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Runtime classifier is tuned to LongMemEval labels (oracle in disguise) | Train on LongMemEval-M held-out set, never -S 500q |
| HyDE regresses temporal (small generator hallucinates dates) | Stage 3 exit gate: temporal ≥ baseline; otherwise drop |
| Rerank inference cost makes full 500q run prohibitive | Pool to k=40 max; use local cross-encoder; amortize across questions |
| Adding shared primitives (rerank_utils, hyde_utils) drags production latency | Both gated default-off; never called from the MCP path unless benchmark opts in |
| Cross-benchmark regression from shared changes | Every main-side change requires both LME + Phase1 audits before merge |
| Reproducibility drift across weeks | Pin answer/judge model IDs; record dataset + code git SHA in every `results.json` |

## 9. Timeline

| Stage | Wall time | Gating |
|---|---|---|
| 0 — baseline | ~3hr | blocking all further work |
| 1 — classifier | 1 day (regex) or 2 days (SLM) | independent |
| 2 — rerank | 1 day (plumbing + judged run) | independent |
| 3 — HyDE trial | 0.5 day | independent |
| 4 — contextual keys | 0.5 day plumbing, depends on Phase1 | blocked on Phase1 PLAN Stage 1 |
| 5 — publish refresh | 0.5 day | after 0-4 |

## 10. References

- Shipped: [`benchmarks/longmemeval/README.md`](./README.md), [`benchmarks/longmemeval/results.json`](./results.json)
- Companion: [`benchmarks/Phase1/PLAN.md`](../Phase1/PLAN.md)
- [LongMemEval ICLR 2025](https://arxiv.org/html/2410.10813v1) — key expansion +4% recall / +5% accuracy; time-aware +11.4% recall on rounds
- [Anthropic Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval) — −49% failure with contextual embeddings+BM25
- Branch rebase-recovery context: memory entry `project_rebase_code_loss.md` — ~1200 lost lines recoverable from `backup-bench-wip-pre-merge` tip `97bc488`
