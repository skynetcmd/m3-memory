# LongMemEval-S (LME-S) Benchmark
-------------------------


2026.05.19 -- Hot off the press
------------------
M3's new smart engine has fresh retrieval numbers for LME-S. Throughout this section the metric is **session-hit-rate@k** — the fraction of questions where at least one of the top-k retrieved turns belongs to the question's gold-evidence session. It is the same metric the public LME-S report quotes (stock M3 = 96.8% @ k=10), so the comparison below is like-for-like.

We're doing accuracy (QA) measurements and will publish those soon. But since so many systems highlight retrieval numbers alongside accuracy, we wanted to give you the data to do an apples-to-apples comparison.

**These are raw-retrieval numbers — no knowledge graph.** Retrieval is over the raw conversation turns: hybrid FTS5 keyword + BGE-M3 vector cosine + MMR diversification, nothing more. There is no knowledge-graph construction, no LLM-extracted entities or relations, and no enrichment layer behind these figures. Retrieval is 100% lossless — the system returns verbatim turns, never a compressed or paraphrased summary. A separate **KG-enriched** leg is in progress and will be published as its own table; this report is the raw baseline it will be measured against.

What this means: on LME-S benchmark data, m3 places a correct-session turn as the **#1** result for **~92%** of questions, and within the top 10 for **99%** — that saves you tokens and time.

# Session-hit-rate@k with the new m3 engine

**Engine:** m3-memory v2026.5.18.1 (`a639ee8c`) + m3-core-rs CUDA, BGE-M3 (Q4_K_M) embeddings, hybrid scoring + MMR diversification, per-instance `conversation_id` scoping.

**Leg:** `m3-search` — raw retrieval, no KG. m3-memory's production `memory_search_scored_impl` over raw turns: hybrid FTS5 + BGE-M3 vector + MMR. No knowledge graph, no LLM enrichment, no oracle category knobs, no smart-retrieval, no rerank.

**N:** 500 instances. Source: `lme_s.db` ingested from `longmemeval_s_cleaned.json`.

---

## Session-hit-rate by k

A hit means at least one of the top-k retrieved turns shares a `session_idx` with one of the question's gold-flagged turns. This is the metric the public LME-S report quotes (stock M3 = 96.8% @ k=10).

| Question type             |   N | k=1   | k=2   | k=3   | k=5   | k=10  | k=20   |
|---------------------------|----:|------:|------:|------:|------:|------:|-------:|
| knowledge-update          |  78 | 97.4% | 98.7% | 98.7% |100.0% |100.0% | 100.0% |
| multi-session             | 133 | 94.0% | 95.5% | 97.7% | 98.5% | 99.2% | 100.0% |
| single-session-assistant  |  56 | 98.2% | 98.2% | 98.2% | 98.2% |100.0% | 100.0% |
| single-session-preference |  30 | 66.7% | 83.3% | 90.0% | 96.7% |100.0% | 100.0% |
| single-session-user       |  70 | 92.9% | 92.9% | 95.7% | 98.6% | 98.6% | 100.0% |
| temporal-reasoning        | 133 | 88.7% | 91.0% | 92.5% | 94.7% | 97.7% | 100.0% |
| **OVERALL**               | **500** | **91.8%** | **94.0%** | **95.8%** | **97.6%** | **99.0%** | **100.0%** |

---

## Turn-hit-rate by k

A stricter variant counts a hit only when at least one of the top-k retrieved turns is *itself* a gold-flagged turn (not merely a turn from the gold session). On LongMemEval-S every gold turn maps to exactly one session, so a question's "first gold hit" rank is identical under both metrics — the turn-hit-rate table is bit-identical to the session-hit-rate table above. The two would diverge only on a corpus where two distinct sessions can both be gold for the same question. No separate table is shown for that reason.

---

## Comparison to the public LME-S report

| Metric | Public LME-S report (stock M3) | New engine (m3-search, this report) | Delta |
|---|---:|---:|---:|
| Session-hit-rate@k=10 | 96.8% | **99.0%** | +2.2pp |

The public report's matched cell is session-hit-rate@k=10 = 96.8%. The new engine reaches **99.0%** at the same (k, metric), and **100%** at k=20.

---

## Method notes

- Retrieval: m3-memory's `memory_search_scored_impl` called per-instance with `conversation_id=instance_id`, k=20, MMR enabled, no recency bias, no adaptive-k, no rerank. All defaults are the m3 production path's defaults except where overridden by the apples-to-apples adapter (`pipeline/m3_search_adapter.py`).
- Embeddings: in-process BGE-M3 GGUF Q4_K_M via `m3_core_rs.EmbeddedEmbedder` (Rust llama.cpp, CUDA), tag `bge-m3-GGUF-Q4_K_M.gguf`. Query and corpus share the same embedder; no cross-space cosine.
- Substrate: `lme_s_m3.db` sidecar built from `lme_s.db` by `pipeline/m3_search_adapter.py build`, holding 246,750 items / 500 conversations / FTS5 + embeddings populated.
- Run wall: ~32 s for 500 instances on a single RTX 5080 at concurrency=1 (the m3-memory search is single-threaded per call).
- Reproducibility: deterministic at temp=0 / no MMR randomness; same DB + same engine SHA produces bit-identical results.

## Acknowledged gaps

- These numbers are **retrieval hit-rate**, not QA accuracy. The public report's headline 89.0% is QA accuracy with a gpt-4o judge. See the Phase 5/6 follow-up for QA numbers on the same retrieval feed.
- **This leg uses no knowledge graph and no LLM enrichment.** Retrieval is embedding-only: hybrid FTS5 + BGE-M3 vector cosine + MMR over the raw turns. The numbers above therefore do not depend on any extraction model.
- A separate **KG-enriched** leg is in progress. To keep the methodology fully local-first, its knowledge graph is being extracted with a local model (Qwen 3.5-9B) rather than a frontier API; those KG-enriched hit-rates will be published as their own table once the extraction completes.
- No rerank, no chain-of-note, no category-aware knobs, no time-aware expansion. These are the public report's "smart-retrieval" levers; this table is the no-knobs base case.

Original LME-S Benchmark report: April 2026
-------------------------------------------
Without retrieval, the answer model scores 6–9%. With M3 Memory's hybrid retrieval, it reaches **89.0%** on [LongMemEval-S](https://github.com/xiaowu0162/LongMemEval) (445/500) — the 500-question long-horizon conversational memory benchmark from Wu et al., 2024.

The 89.0% result uses oracle category metadata from the dataset. Without oracle metadata, accuracy is **74.8%** (smart retrieval with time-aware expansion) to **68.0%** (fixed-k baseline). Both numbers matter: the first measures the retrieval + answer ceiling; the second measures what the system achieves without privileged information.

## Result

| Question type | n | Accuracy |
|---|---|---|
| single-session-user | 70 | 91.4% |
| single-session-assistant | 56 | 94.6% |
| single-session-preference | 30 | 93.3% |
| multi-session | 133 | 85.0% |
| temporal-reasoning | 133 | 86.5% |
| knowledge-update | 78 | 92.3% |
| **Overall** | **500** | **89.0%** |

Answer model: Claude Opus 4.6. Judge: the upstream LongMemEval gpt-4o judge, unmodified — the same judge used in the original paper and on the leaderboard.

**Retrieval session hit-rate at k=10: 96.8%** — the fraction of questions where at least one turn from the gold evidence session appears in the top-10 retrieved turns.

Raw artifact: [`results.json`](./results.json).

## Retrieval contribution

To measure the effect of retrieval, we run the same evaluation with retrieval disabled. Two no-retrieval baselines account for prompt effects: a neutral prompt and a RAG-aware empty-context prompt.

| Question type | n | Neutral prompt | RAG-aware empty | Stock M3 | Delta (vs RAG-aware) |
|---|---|---|---|---|---|
| single-session-user | 70 | 8.6% | 8.6% | 91.4% | +82.8pp |
| single-session-assistant | 56 | 0.0% | 19.6% | 94.6% | +75.0pp |
| single-session-preference | 30 | 3.3% | 0.0% | 93.3% | +93.3pp |
| multi-session | 133 | 8.3% | 9.0% | 85.0% | +76.0pp |
| temporal-reasoning | 133 | 6.0% | 5.3% | 86.5% | +81.2pp |
| knowledge-update | 78 | 7.7% | 7.7% | 92.3% | +84.6pp |
| **Overall** | **500** | **6.4%** | **8.4%** | **89.0%** | **+80.6pp** |

Together these bound the no-retrieval floor. Retrieval supplies the evidence; the answer model reads and reasons over it.

The ss-assistant baseline jumps from 0.0% to 19.6% under the RAG-aware prompt due to a gpt-4o judge artifact: the judge credits natural-phrasing abstentions as correct on 11 non-abstention questions. This adds ~2.2pp to the baseline but does not change the overall conclusion — the no-retrieval floor remains under 10%.

## Category-aware ablation

Having established that retrieval drives the majority of performance, we next examine which retrieval choices matter.

The stock 89.0% run uses oracle category labels from the dataset to select per-category retrieval policies (k values, session expansion, recency bias, role weighting, and answer scaffolds). Removing all category-aware policies drops accuracy from 89.0% → 68.0% (−21pp).

| Question type | n | Stock M3 | No category knobs | Delta |
|---|---|---|---|---|
| single-session-user | 70 | 91.4% | 82.9% | −8.6 |
| single-session-assistant | 56 | 94.6% | 75.0% | −19.6 |
| single-session-preference | 30 | 93.3% | 73.3% | −20.0 |
| multi-session | 133 | 85.0% | 57.1% | −27.9 |
| temporal-reasoning | 133 | 86.5% | 57.1% | −29.4 |
| knowledge-update | 78 | 92.3% | 84.6% | −7.7 |
| **Overall** | **500** | **89.0%** | **68.0%** | **−21.0** |

Session hit-rate remains high (95.2% @ k=10): session-level recall is necessary but insufficient. The drop is concentrated in reasoning-heavy categories (multi-session −27.9pp, temporal −29.4pp), where the system needs broader context assembly — larger k, session expansion, or temporal anchoring — not just finding the right session.

To isolate where the gains come from, we decompose performance relative to the no-retrieval baseline (8.4%):

- **Retrieval alone (no category signals):** 68.0% (+59.6pp)
- **Category-aware policies:** +21.0pp additional

The `--no-category-knobs` flag bundles multiple interacting policies into a single switch. The 21pp effect reflects these interactions; isolating individual contributions (e.g., temporal expansion vs. answer scaffolds) is future work.

Adaptive k selection — trimming retrieved turns by score-distribution elbow, with no oracle metadata — reaches 72.6% (Δ+4.6pp over the no-knobs baseline, within ±0.7pp reproducibility variance).

Smart retrieval — combining adaptive k with time-aware expansion (date-proximity boosting, neighbor-session expansion, temporal-cue detection) — reaches **74.8%** (Δ+6.8pp over no-knobs). The largest gains are in temporal-reasoning (+10.6pp) and multi-session (+6.1pp), the two categories most affected by the knobs removal. The remaining 14.2pp gap to stock M3 is primarily answer-side scaffolds and category-specific k values, not retrieval strategy.

### Retrieval configurations tested

| Config | Category metadata | Adaptive k | Time-aware expansion | Overall |
|---|---|---|---|---|
| Stock M3 | Oracle (from dataset) | No (fixed k=10/20) | No | 89.0% |
| Smart retrieval | None | Yes (elbow trim) | Yes | 74.8% |
| Adaptive-k | None | Yes (elbow trim) | No | 72.6% |
| No category knobs | None | No (fixed k=10) | No | 68.0% |
| No retrieval (RAG-aware) | N/A | N/A | N/A | 8.4% |
| No retrieval (neutral) | N/A | N/A | N/A | 6.4% |

## Method

- **Dataset**: `longmemeval_s_cleaned.json`, 500 instances. Each instance is an isolated conversational history and one question with a known answer.
- **Ingest**: every turn is written to M3 Memory with its session date, role, referenced dates extracted from content, and a `question_id` scope so instances never bleed into each other.
- **Retrieval**: M3 Memory's `memory_search` — hybrid FTS5 keyword + vector cosine + MMR diversity re-ranking. No model trained on LongMemEval.
- **Answer**: Claude Opus 4.6 reads the top retrieved turns and answers using the official LongMemEval per-task prompts.
- **Judge**: the upstream LongMemEval gpt-4o judge, unmodified.

Retrieval uses the same `memory_search_scored_impl` that every M3 Memory agent uses. The benchmark script is a thin driver; there is no shadow retrieval stack.

## Reproduce

```bash
pip install m3-memory
git clone https://github.com/skynetcmd/m3-memory && cd m3-memory

export ANTHROPIC_API_KEY=...
export OPENAI_API_KEY=...

python bin/bench_longmemeval.py                    # stock (89.0%)
python bin/bench_longmemeval.py --smart-retrieval --skip-ingest    # smart retrieval (74.8%)
python bin/bench_longmemeval.py --adaptive-k --skip-ingest         # adaptive-k (72.6%)
python bin/bench_longmemeval.py --no-category-knobs --skip-ingest  # ablation (68.0%)
python bin/bench_longmemeval.py --no-memory        # neutral baseline (6.4%)
python bin/bench_longmemeval.py --rag-aware-empty  # RAG-aware baseline (8.4%)
```

Wall-clock on a single RTX 5080: ~50 min ingest, ~75 min judged answer phase. Baselines and `--skip-ingest` runs reuse an existing DB.

## Caveats

- **Reproducibility**: Claude Opus 4.6 and gpt-4o are non-deterministic at temperature 0. Re-runs produce ≈89.0% ± 0.7pp. Differences under 1pp are noise.
- **Answer model**: this evaluation uses Claude Opus 4.6, a frontier-class model. The baselines show the contribution is retrieval, not parametric knowledge — but a weaker answer model would lower the ceiling.
- **Judge**: single gpt-4o judge, unmodified. No human or secondary LLM validation. The ss-assistant abstention artifact above is one known bias.
- **Oracle metadata**: the stock 89.0% uses dataset category labels. Without them, accuracy drops to 68–75%. A production system would need to infer task signals at runtime.
- **Bundled ablation**: the `--no-category-knobs` flag disables multiple policies simultaneously. Per-knob isolation is not yet available.
- **Cross-system comparisons are uncontrolled**: different systems use different answer models, prompts, judges, and configurations. Scores below are not directly comparable.

## Design space

Long-horizon memory systems make different architectural bets. Cross-system scores are not directly comparable — answer models, prompts, and judges differ.

| System | Architecture | Multi-session | Temporal | Overall | Answer model | Oracle metadata? |
|---|---|---|---|---|---|---|
| [Mastra OM](https://mastra.ai/research/observational-memory) | Ingest-heavy: observer + reflector compression | 87.2% | 95.5% | 94.9% | gpt-5-mini | None |
| M3 Memory (stock) | Retrieval-heavy: raw turns + hybrid search | 85.0% | 86.5% | 89.0% | Opus 4.6 | Category labels |
| M3 Memory (no metadata) | Same, category knobs disabled | 57.1% | 57.1% | 68.0% | Opus 4.6 | None |
| [Ensue](https://ensue.dev/blog/beating-memory-benchmarks/) | Time-aware expansion + configurable windows | — | — | ~86% | Unknown | None |
| [Hindsight](https://github.com/vectorize-io/hindsight) | Reflection pre-pass: LLM writes insights at ingest | 87.2% | — | — | Unknown | None |

**M3**: retrieval at read-time over raw turns. *Upside*: simplicity and zero fidelity loss. *Tradeoff*: query-time evidence assembly, which currently depends on category-aware policies for reasoning-heavy tasks.

**Mastra OM**: ingest-heavy compression with a three-date temporal model. *Upside*: 95.5% temporal reasoning with no retrieval step. *Tradeoff*: ingest cost and compression loss on scattered low-priority facts.

**Ensue**: time-aware retrieval — temporal-cue parsing, date-proximity filtering, and neighbor-session expansion over raw history. *Upside*: proven retrieval-paradigm approach to temporal reasoning without oracle metadata.

Without oracle metadata, smart retrieval (time-aware expansion + adaptive k) recovers roughly a third of the 21pp gap, reaching 74.8%. The remaining 14pp is primarily answer-side scaffolds — closing it within the retrieval paradigm via runtime task inference is the next milestone.

See the [LongMemEval leaderboard](https://github.com/xiaowu0162/LongMemEval#leaderboard) for the current field.
