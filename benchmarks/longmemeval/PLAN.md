# LongMemEval-S Retrieval Improvement Plan

**Branch:** `bench-LME`
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
- Do not add features that require benchmark-specific category names in code.

## 2. Regression-detection contract

Every retrieval change must produce a judged full-500q run before merging to `origin/main`. The following gates apply:

1. **Baseline** is the most recent captured smart-retrieval `results.json` on the target dataset.
2. **Run** the full 500q with `--smart-retrieval --skip-ingest` against the existing DB (reuses ingest; eliminates ingest variance as a confound).
3. **Compare** per-category `accuracy` deltas. A change is blocked if:
   - Any category drops ≥1.5pp (the shipped reproducibility band).
   - `retrieval_session_hit_rate` drops by any amount (mechanical regression).
   - `overall_accuracy` drops ≥1.5pp.
4. **Noise band**: differences under ±1.5pp are treated as noise (Opus 4.6 + gpt-4o are non-deterministic even at temperature 0; published data shows ≈±0.7pp single-direction variance).
5. **Budget**: plan for 2 runs per trialed feature — one A, one A+feature — on the same DB snapshot, same day, same LM Studio / llama-server process.

## 3. Test inventory (what exists, what we add)

### Existing harness bits

- `bin/bench_longmemeval.py` — entry point. Writes `results.json` + `hypotheses.jsonl` + `run.log` to `.scratch/longmemeval_run_<ts>/`.
- `bin/rejudge_*.py` — re-scores an existing hypothesis file under a new judge config (use when judge changes, not when retrieval changes).
- `data/longmemeval/longmemeval_s_cleaned.json` — 500 canonical questions.
- `data/longmemeval/longmemeval_s_strat60.json` — 60-question stratified sample (retrieval smoke only, do not publish).

### Existing flags worth trialing (already wired, not ablation-measured)

| Flag | Source | Status |
|---|---|---|
| `--smart-retrieval` | shipped | published 74.8% |
| `--adaptive-k` | shipped | published 72.6% |
| `--no-category-knobs` | shipped | published 68.0% |
| `--rerank` | Commit 1 of rebase-recovery | untrialed |
| `--hyde` | Commit 1 | untrialed |
| `--rerank-pool-k` | Commit 1 | untrialed |
| `--recency-bias <x>` | Commit 1 | used inside smart-retrieval; standalone untrialed |
| `--smart-neighbor-sessions <n>` | Commit 3 | inside smart-retrieval |
| `--smart-time-boost <w>` | Commit 3 | inside smart-retrieval |
| `--ingest-mode {turn,session}` | Commit 3 | turn shipped; session for ablation only |
| `--reflection` / `--chain-of-note` | Commit 2 | published CoN compare neutral |

### New flags to add (this plan)

| Flag | Stage | Purpose |
|---|---|---|
| `--runtime-category-classifier {off,regex,slm}` | 1 | replace oracle `question_type` with runtime signal |
| `--contextual-keys` | 2 | Anthropic-style contextual prefix on embedded key (not on stored value) |
| `--contextual-bm25` | 2 | same prefix fed into BM25 index |
| `--key-expand-from-turn` | 2 | port of Phase1 `llm_v1_title_ctx` winner (title + session ctx on key) |

### New test artifacts

| Artifact | Location | When written |
|---|---|---|
| `benchmarks/longmemeval/smart_baseline.json` | bench-LME | Stage 0 — captured smart-retrieval per-category baseline |
| `benchmarks/longmemeval/stage1_runtime_classifier.json` | bench-LME | Stage 1 — after classifier |
| `benchmarks/longmemeval/stage2_contextual_keys.json` | bench-LME | Stage 2 — after key-expansion |
| `benchmarks/longmemeval/stage3_temporal_port.json` | bench-LME | Stage 3 — after temporal mechanics land on origin/main path |
| `benchmarks/longmemeval/stage4_*.json` | bench-LME | Stage 4 — rerank/HyDE/contextual-bm25 one-by-one |

Each JSON is the full `results.json` schema; diffs are computed by a small helper to be added: `benchmarks/longmemeval/compare.py` (not yet written; low priority).

## 4. Staged plan

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

**Approach:**
1. Build `bin/question_signal.py` with a pure-regex classifier:
   - `temporal`: date tokens, "when", "how long ago", "before/after", month names.
   - `preference`: "prefer", "favorite", "like/dislike", "rather".
   - `multi-session`: conjunctions across time ("and also", "compared to", "over time").
   - `ss-assistant`: questions about what *the assistant* said/suggested/recommended.
   - Default: `ss-user`.
2. Add `--runtime-category-classifier {off,regex}` to the harness. When `off`, smart-retrieval behaves as published. When `regex`, the classifier output is substituted for the oracle `question_type` at retrieval time.
3. **Training/validation discipline:** hand-write rules against a **held-out 50q sample** from LongMemEval-M (the long variant, not -S). Do not look at the -S 500q when writing rules. This avoids the oracle leak in disguise.
4. Validate classifier agreement with oracle on held-out set (target ≥80% agreement).
5. Run full 500q smart-retrieval with classifier `on`. Record `stage1_runtime_classifier.json`.
6. If overall accuracy lifts ≥3pp over Stage 0 baseline and no category regresses ≥1.5pp: land on `origin/main` behind the flag, default `off`.

**Stretch:** if regex plateau is low, add `--runtime-category-classifier slm` path using a local small model (Qwen 1.5B via llama-server) as zero-shot classifier. Only pursue if regex closes <5pp.

**Exit:** either measured lift ≥3pp on smart-retrieval path, or clean rejection with the reason captured in the results JSON.

### Stage 2 — Contextual key-expansion (Phase1 winner port)

**Hypothesis:** the Phase1 variant report shows `llm_v1_title_ctx` lifts turn-level mean_r@10 by +10.9pp on LoCoMo. The LongMemEval paper reports +4% recall / +5% QA accuracy from fact-augmented keys. These are independent confirmations that key-expansion matters across benchmarks. We don't have it in our LongMemEval stack.

**Approach:**
1. Read `benchmarks/Phase1/runs/audit_20260417_202512/retrieval_trace.jsonl` and the llm_v1_title_ctx ingest code to understand the exact prompt and key format used.
2. Add `--key-expand-from-turn` flag to `bin/bench_longmemeval.py`. When set, ingest emits memory items whose **embedded key** is `<title_prefix> | <session_ctx> | <raw_turn>`, while the stored **value** stays raw.
3. Paper discipline: **expand keys, not values**. Values stay verbatim to preserve single-session category accuracy.
4. Re-ingest under variant `lme-contextual-strat60` (stratified 60q) first as a cheap smoke; if no regression, re-ingest full 500q under variant `lme-contextual-500`.
5. Judged full 500q run. Record `stage2_contextual_keys.json`.
6. Regression gate from Section 2.

**Cost:** ingest re-runs (~1hr each on 5080 + llama-server), plus 1 full judged run (~2hr).

**Exit:** per-category delta committed. If ss-* categories regress ≥1.5pp, revert; key expansion is meant to lift multi-session and temporal without touching ss-*.

### Stage 3 — Port temporal mechanics to origin/main

**Problem:** `extract_referenced_dates`, `has_temporal_cues`, smart-time-boost (±30d vs `valid_from`, ±14d vs `referenced_dates`), neighbor-session expansion are on `bench-LME` but **not on `origin/main`**. Production agents don't get the +10.6pp temporal lift that smart-retrieval provides on this benchmark.

**Approach:**
1. Cherry-pick (or re-apply manually) the three temporal helpers from `bench-LME` `bin/bench_longmemeval.py` into `bin/memory_core.py` on `origin/main`. The production agent path, not the bench harness, is what needs this.
2. Gate behind a feature flag or new retrieval mode (not a new flag on every call — agents should not have to opt in).
3. Run LongMemEval smart-retrieval on a branch with the port applied. Confirm: temporal-reasoning accuracy does not regress vs Stage 0 baseline; overall does not regress.
4. Separately, re-run LoCoMo phase1 audit on the same branch (see LoCoMo PLAN.md Stage 2).

**Exit:** temporal mechanics ship on `origin/main` with a LongMemEval regression check and a LoCoMo retrieval-audit check both passing.

### Stage 4 — Trial unmeasured levers one at a time

Only after Stages 0-3 have landed a measured new baseline. Each trial is a single-flag A/B against the new baseline:

| Flag | Expected lever | Risk |
|---|---|---|
| `--rerank --rerank-pool-k 40` | cross-encoder reranks pool of 40 → top-k; Anthropic reports +13pp from rerank on top of contextual retrieval | extra LLM call per question; may regress on ss-* if pool is too diverse |
| `--hyde` | HyDE query expansion; paper warns weak LLMs hallucinate time cues | regression on temporal if generator is small |
| `--contextual-bm25` | feeds Stage 2's contextual prefix into BM25 index | requires Stage 2 landed |
| `--recency-bias 0.1 --no-category-knobs` | isolated recency contribution | diagnostic, not a production lever |

Each gets its own full 500q judged run. Drop any that don't beat ±1.5pp. Stack only winners.

### Stage 5 — Publish refresh

Once Stages 0-3 produce a measurable smart-retrieval lift, update `benchmarks/longmemeval/README.md`:

- Add smart-retrieval per-category table (closes the audit gap).
- Reframe the 74.8% → new number as the realistic ceiling.
- Keep 89.0% as the oracle-metadata upper bound.
- Add the new ablation rows (runtime classifier, contextual keys).
- Update the "14.2pp gap" paragraph to reflect what closed and what remains.

Draft the README update on `bench-LME`, then PR to `main` after sign-off.

## 5. Test procedures (how-to)

### A — Smart-retrieval per-category run

```bash
# from repo root on bench-LME (or any branch with the Commit 1-3 harness)
python bin/bench_longmemeval.py \
  --smart-retrieval --skip-ingest \
  --dataset data/longmemeval/longmemeval_s_cleaned.json --limit 500 \
  --k 10 --k-reasoning 20 \
  --answer-model claude-opus-4-6 --judge-model gpt-4o \
  2>&1 | tee .scratch/lme_smart_$(date +%Y%m%d_%H%M%S).log
```

Artifacts end up in `.scratch/longmemeval_run_<ts>/results.json`. Copy to `benchmarks/longmemeval/` under a descriptive name for each stage.

### B — Strat60 retrieval smoke (use for pre-flight checks only)

```bash
PYTHONIOENCODING=utf-8 EVAL_GENERATOR_MODEL=stub python -m bin.bench_longmemeval \
  --dataset data/longmemeval/longmemeval_s_strat60.json \
  --limit 60 --skip-ingest --no-judge --k 10 \
  --smart-retrieval 2>&1 | tail -40
```

No judge, no answerer — only measures session_hit_rate and whatever retrieval signals `run.log` prints. Do not publish from this.

### C — Per-category delta diff

Quick shell pattern until `compare.py` exists:

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

Any run that violates the Section 2 contract:

1. Save the offending `results.json` as `regressions/<branch>-<ts>.json`.
2. Open an issue or log under `benchmarks/longmemeval/regressions.md`.
3. Do not merge until root cause is identified or the feature is reverted.

### E — Reproducibility verification

Before drawing any conclusion from a sub-2pp delta, re-run the same config once more. Published band is ±0.7pp; confirm the delta sign holds across both runs.

## 6. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Runtime classifier is tuned to LongMemEval labels (oracle in disguise) | Train on LongMemEval-M held-out set, never -S 500q |
| Key expansion regresses single-session-* (paper-documented risk for value-level extraction) | Apply to keys only; verify ss-* categories hold within ±1.5pp |
| Temporal port breaks production agents | Feature-flag the port; run existing integration tests + smart-retrieval regression before merging to main |
| Reprouducibility drift across weeks (model updates) | Pin answer/judge model IDs; record dataset + code git SHA in every `results.json` |
| Strat60 smoke misleads | This plan does not ship anything off strat60 numbers; they only pre-flight flag wiring |

## 7. Timeline

Rough, assuming one focused session per stage, local GPU available:

| Stage | Wall time | Gating |
|---|---|---|
| 0 — baseline | ~3hr | blocking all further work |
| 1 — classifier | 1 day (regex) or 2 days (SLM path) | blocks stage 5 publish refresh |
| 2 — contextual keys | 1 day (ingest + judge) | independent of 1 |
| 3 — temporal port to main | 0.5 day (plumbing) | origin/main smoke + LoCoMo audit |
| 4 — each lever | 2-3hr per lever | stack only winners |
| 5 — publish refresh | 0.5 day | after 0-3 |

## 8. References

- Shipped: [`benchmarks/longmemeval/README.md`](./README.md), [`benchmarks/longmemeval/results.json`](./results.json)
- LoCoMo companion plan: [`benchmarks/Phase1/PLAN.md`](../Phase1/PLAN.md) (on bench-Phase1 branch)
- Paper: [LongMemEval ICLR 2025](https://arxiv.org/html/2410.10813v1) — key expansion +4% recall / +5% accuracy; time-aware +11.4% recall on rounds
- [Anthropic Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval) — −49% failure with contextual embeddings+BM25
- [Supermemory research](https://supermemory.ai/research/), [Mastra Observational Memory](https://mastra.ai/research/observational-memory)
- Phase1 variant data: [`benchmarks/Phase1/VARIANT_REPORT_5x500.md`](../Phase1/VARIANT_REPORT_5x500.md) (on bench-Phase1 branch)
- Branch rebase-recovery context: memory entry `project_rebase_code_loss.md` — ~1200 lost lines recovered from `backup-bench-wip-pre-merge`
