# LongMemEval-S Benchmark

**89.0% on LongMemEval-S with stock retrieval.** M3 Memory's hybrid BM25 + vector + MMR stack over local SQLite, evaluated against [LongMemEval-S](https://github.com/xiaowu0162/LongMemEval) — the 500-question long-horizon conversational memory benchmark from Wu et al., 2024. Without retrieval, the answer model scores 6–9%.

The 89.0% result uses oracle category metadata from the dataset. Without oracle metadata, accuracy is **74.8%** (smart retrieval with time-aware expansion) to **68.0%** (fixed-k baseline). Both numbers matter: the first measures the retrieval + answer ceiling; the second measures what the system achieves without privileged information.

## Result

**89.0% overall** (445/500), using M3 Memory's stock hybrid retrieval, Claude Opus 4.6 as the answer model, and the upstream LongMemEval gpt-4o judge unmodified — the same judge used in the original paper and on the leaderboard. Using a weaker/cheaper judge (e.g. gpt-oss) is a known way to inflate LongMemEval scores; we did not do that.

| Question type | n | Accuracy |
|---|---|---|
| single-session-user | 70 | 91.4% |
| single-session-assistant | 56 | 94.6% |
| single-session-preference | 30 | 93.3% |
| multi-session | 133 | 85.0% |
| temporal-reasoning | 133 | 86.5% |
| knowledge-update | 78 | 92.3% |
| **Overall** | **500** | **89.0%** |

**Retrieval session hit-rate at k=10: 96.8%** — the fraction of questions where at least one turn from the gold evidence session appears in the top-10 retrieved turns. This is an automatic metric computed against LongMemEval's per-question `answer_session_ids`, not an LLM judgment. It's a loose proxy: a "hit" means the right session is in the top-10, not that the specific answer-bearing turn is. Use it to bound retrieval recall from below, not to claim turn-level precision.

Raw artifact: [`results.json`](./results.json).

## Isolating the retrieval contribution

To isolate what the retrieval stack actually contributes, we ran the same 500 questions with retrieval disabled — Claude Opus 4.6 answers with no turns retrieved from M3. Same answer model, same judge, same dataset. The only variable is whether the retrieval pipeline runs.

We ran two baseline framings, because the choice of system prompt is itself a confound. The first uses a neutral prompt that makes no reference to memory. The second uses M3's real RAG system prompt (the same one the stock pipeline uses) but feeds it an empty history block — the model thinks it has a RAG pipeline and sees that the retriever returned zero results. Both are legitimate baselines; they answer slightly different questions and together bracket the null-retriever floor.

| Question type | n | Neutral prompt | RAG-aware empty | Stock M3 | Delta (vs RAG-aware empty) |
|---|---|---|---|---|---|
| single-session-user | 70 | 8.6% | 8.6% | 91.4% | +82.8pp |
| single-session-assistant | 56 | 0.0% | 19.6% | 94.6% | +75.0pp |
| single-session-preference | 30 | 3.3% | 0.0% | 93.3% | +93.3pp |
| multi-session | 133 | 8.3% | 9.0% | 85.0% | +76.0pp |
| temporal-reasoning | 133 | 6.0% | 5.3% | 86.5% | +81.2pp |
| knowledge-update | 78 | 7.7% | 7.7% | 92.3% | +84.6pp |
| **Overall** | **500** | **6.4%** | **8.4%** | **89.0%** | **+80.6pp** |

Both baselines land in the 6–9% range on every category except `single-session-assistant`, and the stock retrieval stack lifts that floor by 75–93pp depending on category. The retrieval layer is supplying the evidence the answer model reasons over; parametric knowledge alone clears less than one question in ten.

### Why the ss-assistant column jumps from 0.0% to 19.6%

The two baselines disagree on one category, and the cause is worth calling out.

Under the neutral prompt, when Opus has no memory it is instructed to reply exactly *"I don't know based on our past conversations."* That rigid form doesn't earn credit from the gpt-4o judge on non-`_abs` `single-session-assistant` questions. Under the RAG-aware prompt — the same one the stock pipeline uses — Opus instead produces natural variations like *"I don't have any memories from past conversations about X."* On 11 of the 56 `single-session-assistant` questions, the judge marks those variations correct even though the reference answer is a specific fact like `"By Chloe"` or `"Memrise"`. These are not parametric-knowledge hits; they are a judge calibration artifact in which the judge credits honest abstention on questions the benchmark didn't mark as abstention-eligible.

The artifact adds 11 "correct" answers to the RAG-aware baseline, or ~2.2pp overall. It does not change the story: even after pocketing every one of those 11 judge-credit hits, the baseline is still 8.4% and retrieval contributes +80.6pp overall and +75pp on the affected category. Of the 42 questions Opus answers "correctly" with no memory under the RAG-aware prompt, **30 are `_abs` abstention-credit** (the expected floor — LongMemEval rewards honest "I don't know" on `_abs` variants), **11 are the judge artifact on ss-assistant**, and **1 is a coincidental hit on temporal-reasoning**. Zero are genuine parametric-knowledge retrievals on a question that rewards one.

Reproduce either baseline:

```bash
python bin/bench_longmemeval.py --no-memory          # neutral-prompt baseline (6.4%)
python bin/bench_longmemeval.py --rag-aware-empty    # RAG-aware empty-context baseline (8.4%)
```

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

### Category-aware retrieval knobs

`memory_search` exposes category-aware retrieval parameters — reasoning-heavy question types get a larger `k`, assistant-authored content gets a small role boost at re-rank time, a few categories get a mild recency bias. These are production knobs available to every M3 agent, not LongMemEval-specific branches.

But the honest caveat is: in this benchmark run, the question category is supplied as ground-truth metadata from the dataset. A real agent at runtime would have to infer the analogous task context on its own (or carry it from whatever spawned the task). We believe the mapping "reasoning-heavy task ⇒ larger k" is robust across task types, not tuned to LongMemEval's specific category labels, and the ablation above (68.0% → 74.8% via smart retrieval) supports that. Closing the remaining 14.2pp gap within the retrieval paradigm — via runtime task inference rather than oracle metadata — is the next milestone.

## Reproduce

```bash
# install
pip install m3-memory
git clone https://github.com/skynetcmd/m3-memory && cd m3-memory

# fetch dataset (out of band — LongMemEval-S is ~265 MB)
# place at: data/longmemeval/longmemeval_s_cleaned.json

# run embedding server (llama-server with any Qwen3-0.6B embedding GGUF)
# or point LLM_ENDPOINTS_CSV at your preferred OpenAI-compatible endpoint

# set API keys
export ANTHROPIC_API_KEY=...   # answer model
export OPENAI_API_KEY=...      # judge model

# run the full 500 in one shot
python bin/bench_longmemeval.py                                    # stock (89.0%)
python bin/bench_longmemeval.py --smart-retrieval --skip-ingest    # smart retrieval (74.8%)
python bin/bench_longmemeval.py --adaptive-k --skip-ingest         # adaptive-k (72.6%)
python bin/bench_longmemeval.py --no-category-knobs --skip-ingest  # ablation (68.0%)
python bin/bench_longmemeval.py --no-memory                        # neutral baseline (6.4%)
python bin/bench_longmemeval.py --rag-aware-empty                  # RAG-aware baseline (8.4%)
```

Artifacts land in `.scratch/longmemeval_run_<timestamp>/`:
- `hypotheses.jsonl` — one line per question
- `results.json` — aggregate accuracy and per-type breakdown
- `run.log` — per-question progress

Wall-clock on a single RTX 5080: ~50 min for the ingest phase, ~75 min for the judged answer phase. Baselines and `--skip-ingest` runs reuse an existing DB.

## Honest caveats

- **Reproducibility band**: Claude Opus 4.6 and gpt-4o are both non-deterministic in practice, even at temperature 0. Re-running the same config produces ≈89.0% ± 0.7pp. Differences under ~1.5pp should be treated as noise.
- **Answer model dependency**: this evaluation uses a frontier-class answer model. The no-memory baselines (6.4% and 8.4%) show that the contribution is retrieval, not parametric knowledge — but swapping Opus 4.6 for a weaker answer model would lower the ceiling. M3 Memory itself runs entirely locally; the answer LLM is a benchmark convention, not a runtime requirement.
- **Judge choice**: we use the upstream LongMemEval gpt-4o judge, the same judge the original paper uses. We did not run a second judge to cross-validate, and LLM-as-judge has known biases. The ss-assistant artifact above is one such bias showing up in our own data.
- **Oracle metadata**: the stock 89.0% hands the dataset's question category to the retrieval layer. A real agent would have to derive the analogous task context on its own. See the "Category-aware retrieval knobs" section above and the smart-retrieval ablation (74.8% without oracle metadata).
- **Bundled ablation**: the `--no-category-knobs` flag disables multiple policies simultaneously. Per-knob isolation is future work.
- **Cross-paper comparisons are uncontrolled**: the architectural notes below are design-space context, not head-to-head reruns. Different papers use different answer models, prompts, and dataset snapshots.

## The design space

Long-horizon memory systems have converged on a few distinct architectural bets. The interesting question is which bet wins on which question type. Cross-system scores below are not directly comparable — answer models, prompts, judges, and metadata availability differ.

| System | Architecture | Multi-session | Temporal | Overall | Answer model | Oracle metadata? |
|---|---|---|---|---|---|---|
| [Mastra OM](https://mastra.ai/research/observational-memory) | Ingest-heavy: observer + reflector compression | 87.2% | 95.5% | 94.9% | gpt-5-mini | None |
| M3 Memory (stock) | Retrieval-heavy: raw turns + hybrid search | 85.0% | 86.5% | 89.0% | Opus 4.6 | Category labels |
| M3 Memory (no metadata) | Same, category knobs disabled | 57.1% | 57.1% | 68.0% | Opus 4.6 | None |
| [Ensue](https://ensue.dev/blog/beating-memory-benchmarks/) | Time-aware expansion + configurable windows | — | — | ~86% | Unknown | None |
| [Hindsight](https://github.com/vectorize-io/hindsight) | Reflection pre-pass: LLM writes insights at ingest | 87.2% | — | — | Unknown | None |
| [Memento](https://arxiv.org/abs/2410.05983) | Structured bitemporal entity graph at ingest | — | — | — | — | — |

**M3 Memory** stores raw turns in local SQLite and does the work at *retrieval* time: BM25 + vector + MMR, no entity graph, no reflection pass. *Upside*: simplicity and zero fidelity loss. *Tradeoff*: query-time evidence assembly, which currently depends on category-aware policies for reasoning-heavy tasks.

**Mastra OM** does ingest-heavy compression with a three-date temporal model. *Upside*: 95.5% temporal reasoning with no retrieval step. *Tradeoff*: ingest cost and compression loss on scattered low-priority facts.

**Ensue** does time-aware retrieval — temporal-cue parsing, date-proximity filtering, and neighbor-session expansion over raw history. *Upside*: proven retrieval-paradigm approach to temporal reasoning without oracle metadata.

**Hindsight** runs a reflection pre-pass: before a raw experience is stored, an LLM reads it and writes higher-level insights. This trades extra ingest-time compute for richer stored content, and it's a sensible bet on reasoning-heavy question types.

**Memento** builds a structured bitemporal entity graph at ingest time. The upside is explicit entity disambiguation and conflict resolution — useful when the benchmark tests whether the system can reconcile "my dog's name was Rex / my dog's name is Max" across sessions. The cost is ingest-time complexity and a separate graph store to operate.

Without oracle metadata, M3's smart retrieval (time-aware expansion + adaptive k) recovers roughly a third of the 21pp gap, reaching 74.8%. The remaining 14pp is primarily answer-side scaffolds — closing it within the retrieval paradigm via runtime task inference is the next milestone.

M3's 89.0% on LongMemEval-S says the retrieval-at-read-time bet is viable for long-horizon memory. It doesn't say M3 is "better" than graph- or reflection-based approaches — those would need a controlled rerun with matched answer models and prompts. See the [LongMemEval leaderboard](https://github.com/xiaowu0162/LongMemEval#leaderboard) for the current field.
