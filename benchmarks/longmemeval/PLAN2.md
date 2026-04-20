# LongMemEval-S Performance Maximization Plan (PLAN2)

**Status:** Draft / Consolidated
**Core Goal:** Close the 14.2pp gap between "Smart Retrieval" (74.8%) and "Oracle/Stock" (89.0%) without using privileged metadata.
**Efficiency Mandate:** Minimize tokens, maximize local speed, and ensure future reusability of ingested data.

## 1. Strategy Overview

The strategy shifts from "tuning individual knobs" to "robust runtime inference." We will build a pipeline that:
1.  **Reuses** a canonical ingested SQLite DB to eliminate ingest cost/variance.
2.  **Infers** the question type using a hybrid Regex + k-NN embedding classifier.
3.  **Applies** the optimal retrieval policy (k, time-boost, role-weight, expansion) based on the inferred type.
4.  **Refines** the result set using a local cross-encoder reranker.
5.  **Evaluates** using token-minimal local infrastructure where possible.

---

## 2. Phase 0: The "Golden DB" (Speed & Reusability)

Ingestion is the most expensive part of the benchmark in terms of time and tokens. We will perform it once and freeze it.

### 2.1 One-Time Ingest
Execute a full ingestion of the 500-question dataset with the standard `qwen3-embedding` model.
```bash
# Set your local embedder
export EMBED_MODEL=qwen3-embedding:0.6b-q8

# Ingest once (full 500 questions)
python bin/bench_longmemeval.py --ingest-only --skip-judge

# Backup the resulting DB
cp memory/agent_memory.db memory/lme_golden_baseline.db
```

### 2.2 Reusable Benchmark Flow
For all subsequent runs, use:
`--skip-ingest --variant <BASELINE_TAG>`
This ensures every experiment runs against the exact same vectors, removing ingestion noise.

---

## 3. Phase 1: High-Fidelity Runtime Classifier (+8–12pp Target)

The largest performance gap comes from not knowing if a question is `temporal-reasoning`, `knowledge-update`, or `ss-preference`.

### 3.1 Hybrid Classifier Implementation
Enhance the existing `bin/task_classifier.py` and `bin/bench_longmemeval.py` logic:
1.  **Regex First (Fast/Cheap)**: Use the refined patterns in `classify_question_lme_regex` for unambiguous signals (dates, names, explicit preference words).
2.  **k-NN Fallback (Semantic)**: Use the 3000-exemplar weighted k-NN classifier already in `bin/task_classifier.py` for ambiguous queries.
3.  **Consolidated Output**: Map the 6 LME types to their optimal retrieval configurations.

### 3.2 Type-to-Policy Mapping (The "Smart" in Smart-Retrieval)
| Inferred Type | Retrieval Policy (Knobs) |
|---|---|
| `temporal-reasoning` | k=30, time-boost=0.20, neighbor-sessions=3, temporal-system-prompt |
| `knowledge-update` | k=15, recency-bias=0.25, update-system-prompt |
| `single-session-preference` | k=10, role-boost (user), preference-system-prompt |
| `single-session-assistant` | k=10, role-boost (assistant), session-expansion |
| `multi-session` | k=25, neighbor-sessions=2, graph-depth=1 |

---

## 4. Phase 2: Local Reranking & Refinement (+3–5pp Target)

Once we have a candidate pool of `k*2` or `k*3`, we use a local discriminative model to ensure the "needle" is at the top.

### 4.1 Cross-Encoder Rerank
1.  Implement `bin/rerank_utils.py` (shared utility).
2.  Use `cross-encoder/ms-marco-MiniLM-L-6-v2` (small, fast, CPU-friendly).
3.  Add `--rerank --rerank-pool-k 40` to `bench_longmemeval.py`.
4.  Reranking converts the "retrieval session hit rate" (currently ~96%) into high-quality context for the generator.

---

## 5. Phase 3: Token-Minimal Evaluation Pipeline

To allow rapid iteration without burning API credits:

### 5.1 Local Generation & Judging
1.  **Local Generator**: Use `Llama-3-8B-Instruct` or `Qwen2.5-7B-Instruct` via LM Studio / llama-server for "pre-flight" runs.
2.  **Local Judge**: Use a 14B+ local model (e.g., `Mistral-Small`) for rough accuracy checks.
3.  **Final Gate**: Run the full `Claude-3.5-Opus` + `GPT-4o` judge only when a configuration passes the local pre-flight with a delta of >2pp.

### 5.2 Hypothesis Caching
Add a `--cache-hypotheses` flag to `bench_longmemeval.py`:
- Store `(question_id, config_hash) -> hypothesis`.
- If we rerun the same retrieval/generator config, reuse the answer. Skip tokens.

---

## 6. Regression & Integration Standards

1.  **Shared Levers**: Any change to `memory_core.py` (e.g., `recency_bias` implementation) must be gated by a flag and verified against `Phase1` (LoCoMo) benchmarks to ensure no side-effects.
2.  **Benchmark Cleanliness**: LME-specific category names must never leak into `memory_core.py`. They are translated to numeric/boolean knobs in `bench_longmemeval.py`.
3.  **Result Artifacts**:
    - `benchmarks/longmemeval/smart_baseline.json`: Capture of current 74.8% state.
    - `benchmarks/longmemeval/PLAN2_stage1.json`: Lift after Hybrid Classifier.
    - `benchmarks/longmemeval/PLAN2_stage2.json`: Lift after Rerank.

---

## 7. Timeline & Expected Gains

| Stage | Action | Expected Lift | Wall Time |
|---|---|---|---|
| **0** | Capture Baseline (74.8%) | 0.0pp | 2 hrs |
| **1** | Hybrid Classifier | +6–8pp | 1 day |
| **2** | Adaptive Knobs | +2–4pp | 0.5 day |
| **3** | Local Rerank | +2–3pp | 1 day |
| **Total** | | **85% - 87%** | **3 days** |

---

## 8. Next Steps (Pending Approval)

1.  **Approve PLAN2.md**.
2.  Perform Golden DB ingestion.
3.  Implement `bin/rerank_utils.py`.
4.  Refine `bin/bench_longmemeval.py` to use the Hybrid Classifier as default for `--smart-retrieval`.
