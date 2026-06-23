# LME-S Benchmark – m3-Memory v3

**92.0% question accuracy · 100% retrieval session hit-rate @ k=20**
*No oracle data · Opus 4.6 answerer · GPT-4o judge*

> Benchmark harnesses live in `benchmarks/` and require a repository checkout; see [`CONTRIBUTING.md`](../../CONTRIBUTING.md). They are not shipped on PyPI.
> Results are based on m3-memory engine `v2026.6.8.2-22-gaf65067` (commit `af650678`).
> The earlier oracle-routed results are preserved at [`LME-S_v1_Benchmarking_Report.md`](LME-S_v1_Benchmarking_Report.md) (v1, 89.0% with oracle category metadata).


This report evaluates the production **no-oracle** LongMemEval-S configuration for m3-memory. Unlike the [v1 report](LME-S_v1_Benchmarking_Report.md), which used oracle dataset `question_type` labels for routing, this v3 configuration infers all routing signals from the question text at runtime and does not use privileged dataset metadata.

On [LongMemEval-S](https://github.com/xiaowu0162/LongMemEval), v3 achieves:

> **92.0% accuracy — 460/500**

This exceeds the v1 oracle headline of **89.0%**, but it is not a clean single-variable ablation. The v3 run differs from v1 in answer model, prompts, routing, and retrieval configuration.

## Two different metrics — retrieval accuracy vs. end-to-end QA accuracy

LongMemEval-S is reported with two metrics that measure fundamentally different things. Vendor pages routinely blur them; this report keeps them strictly separate.

| | **Retrieval accuracy (SHR / recall@k)** | **End-to-end QA accuracy** |
|---|---|---|
| **What it measures** | Did the memory layer surface a turn from the correct evidence session within the top-k results? | Did the full pipeline (retrieve → route → answer) produce the *judged-correct* answer? |
| **Depends on** | The memory system only — retrieval + ranking. **No answer model involved.** | Memory system **and** the answer model, prompts, and judge. |
| **Isolates** | The substrate. This is the like-for-like number that actually compares memory systems. | The whole stack. Heavily influenced by the reader LLM, so it is **not** a clean memory-layer measure. |
| **m3 v3 result** | **99.2% @ k=10 (496/500), 100% @ k=20** | **92.0% (460/500)** |

**Retrieval accuracy — m3's core strength.** On the binary per-question `recall_any@k` convention (the "R@k" the adjacent LongMemEval submissions report), the v3 core engine reaches **99.2% session-hit-rate @ k=10 and 100% @ k=20** — raw turns, hybrid FTS5 + BGE-M3 vector + MMR, no knowledge graph, no oracle metadata. The right evidence session is in the top-10 for >99% of questions and in the top-20 for **all 500**. This is state-of-the-art for a fully local-first substrate and is the metric that isolates what the memory layer actually does. (Per-question-type SHR is in the [Retrieval](#retrieval) section; its overall k=10 figure is 99.4% under a slightly different per-type aggregation — both round to ~99%.)

**End-to-end QA accuracy — strong, but answer-model-dependent.** With **no oracle metadata** (routing inferred from the question text at runtime), the full v3 configuration scores **92.0% (460/500)** using a frontier answerer (Opus 4.6) and the unmodified upstream gpt-4o judge. Because this number rides on the reader LLM, it should only be compared against *other systems' QA-accuracy figures*, never against their retrieval/recall numbers.

**Why the gap, and why it matters.** The distance between **100% SHR @ k=20** and **92.0% QA** is the honest signal: when the correct evidence session is present for every question, the remaining ~8% of errors are **answer-side** — evidence-span selection, computation, temporal ordering, preference inference, formatting, and abstention — **not** retrieval failures. A memory layer that can't find what's there is a liability; m3's retrieval essentially removes that failure mode, leaving the residual error where it belongs: in the reader model. See [Retrieval](#retrieval) for the full breakdown.

## Summary

| Configuration | Oracle Labels? | Answerer | Routing | Accuracy |
|---|---:|---|---|---:|
| v1 | Yes | Opus 4.6 | Oracle `question_type` | 89.0% |
| v3 | No | Opus 4.6 | Inferred 4-way strategy router | **92.0%** |

v3 uses:

- **Answerer**: Opus 4.6
- **Routing**: inferred 4-way strategy classifier: `FACT`, `COMPUTE`, `PROSE`, `ASSISTANT`
- **Prompting**: strategy-specific frontier prompts
- **Retrieval**: v3 production `combined-cf` precision-L3 surface
- **Judge**: GPT-4o using the unmodified upstream LongMemEval judge

The inferred router reaches **91.4% agreement** with the canonical post hoc mapping from LongMemEval question types to strategies. This agreement score is reported for analysis only; oracle labels are not used during answering.

One example, `bc8a6e93_abs`, produced non-JSON output and was counted as incorrect. The reported 92.0% is therefore a conservative score under the current harness behavior.

---

## Results by Canonical Question Type

| Question Type | n | v3 Accuracy — No Oracle | v1 Accuracy — with Oracle |
|---|---:|---:|---:|
| single-session-user | 70 | 94.3% — 66/70 | 91.4% |
| single-session-assistant | 56 | 96.4% — 54/56 | 94.6% |
| single-session-preference | 30 | **80.0% — 24/30** | 93.3% |
| multi-session | 133 | 87.2% — 116/133 | 85.0% |
| temporal-reasoning | 133 | 95.5% — 127/133 | 86.5% |
| knowledge-update | 78 | 93.6% — 73/78 | 92.3% |
| **Overall** | **500** | **92.0% — 460/500** | **89.0%** |

The largest improvements over v1 are in reasoning-heavy categories, especially **temporal-reasoning**. The main regression is in **single-session-preference**, where v3 drops from 93.3% to 80.0%. This suggests that preference-style questions remain the weakest area for the no-oracle strategy-routed configuration.

---

## Accuracy by Inferred Strategy

The table below reports accuracy by the strategy inferred at runtime. These buckets are not identical to the canonical LongMemEval question-type buckets because routing is performed from question text only.

| Inferred Strategy | n | Correct | Accuracy |
|---|---:|---:|---:|
| COMPUTE | 273 | 255 | 93.4% |
| FACT | 143 | 129 | 90.2% |
| ASSISTANT | 56 | 54 | 96.4% |
| PROSE | 28 | 22 | 78.6% |
| **Overall** | **500** | **460** | **92.0%** |

`COMPUTE` performs strongly, consistent with the gains observed on temporal and multi-session reasoning questions. `ASSISTANT` is also high-performing. `PROSE`, which primarily handles preference-like inference, remains the most challenging strategy bucket.

---

## How many errors come from routing?

Because routing is inferred at runtime (no oracle labels), a natural question is how much of the 8% error rate is attributable to the router sending a question to the wrong strategy bucket, versus answer-side behavior.

Of the **40 errors** (out of 500), **5 (12.5%) were misrouted** — in every case a `COMPUTE` question (multi-session or temporal) that the regex router placed in the `FACT` bucket:

| Question type | Canonical strategy | Inferred strategy | Count |
|---|---|---|---|
| multi-session / temporal | COMPUTE | FACT | 5 |

Inspecting each misrouted failure, only **~2–3 are plausibly *caused* by the misroute** — the cases that needed `COMPUTE`'s sum/enumerate behavior (e.g. "page count of the **two** novels," answered as two separate numbers rather than summed). The remaining misrouted cases fail for reasons independent of routing: a simple stated-fact lookup the wrong value was read from (where `FACT` is arguably the correct bucket anyway), and one unanswerable `_abs` question that should have been abstained on (an abstention failure, not a routing failure).

So **routing mistakes account for at most 12.5% of errors (5/40), and realistically ~7.5% (3/40)** once miscause is excluded. The other ~88–93% of errors are answer-side — evidence interpretation, computation, preference inference, and abstention — consistent with the near-ceiling retrieval result. The inferred router (91.4% post-hoc agreement) is **not the dominant error source**; closing the answer-side gap (especially preference inference) is the larger opportunity.

---

## Retrieval

The answer surface is the v3 production `combined-cf` precision-L3 leg. Session Hit-Rate (SHR) — the fraction of questions where at least one turn from the gold evidence session appears in the top-k retrieved turns — for the production search engine (`m3-search`) by question type:

| Question type | n | k=5 | k=10 | k=20 | k=50 |
|---|---|---|---|---|---|
| single-session-user | 70 | 98.6% | 100.0% | 100.0% | 100.0% |
| single-session-assistant | 56 | 100.0% | 100.0% | 100.0% | 100.0% |
| single-session-preference | 30 | 100.0% | 100.0% | 100.0% | 100.0% |
| multi-session | 133 | 98.5% | 99.2% | 100.0% | 100.0% |
| temporal-reasoning | 133 | 97.7% | 98.5% | 100.0% | 100.0% |
| knowledge-update | 78 | 100.0% | 100.0% | 100.0% | 100.0% |
| **Overall** | **500** | **98.8%** | **99.4%** | **100.0%** | **100.0%** |

SHR reaches **100% @ k=20** for every question type — for all 500 examples, a turn from the relevant session is present in the top-20 retrieved turns (matching the published v1 SHR; LongMemEval issue #43).

SHR is a session-level recall metric: it confirms the relevant session is in the retrieved context, but not that the answerer selects the correct evidence span, distinguishes updated facts, performs the required computation, resolves temporal ordering, infers preferences, or abstains when unsupported. The gap between **100% SHR @ k=20** and **92.0% QA accuracy** therefore points to answer-side limitations — evidence interpretation, computation, temporal reasoning, preference inference, formatting, and abstention — rather than retrieval failure. This is consistent with the routing-error analysis above: routing accounts for at most ~12.5% of errors, and the remainder are answer-side.

## Method

### Dataset

- Benchmark: LongMemEval-S
- Size: 500 examples
- Evaluation target: exact LongMemEval judge outcome using the unmodified upstream LongMemEval judge harness (GPT-4o)

### Retrieval

- Surface: `combined-cf`
- Retrieval profile: precision-L3
- Session Hit-Rate: 100% @ k=20

### Routing

- Router: regex-based strategy classifier in `pipeline/strategy_router.py`
- Runtime inputs: question text only
- Oracle metadata: not used
- Post hoc agreement with canonical mapping: 91.4%

The router maps questions into four answer strategies:

- `FACT`
- `COMPUTE`
- `PROSE`
- `ASSISTANT`

### Answering

- Model: Opus 4.6
- Prompt set: `config/answerer_prompts/lme_strategy_frontier.yaml`
- Prompting: per-strategy frontier prompts
- Oracle labels: not used

### Judging

- Judge: GPT-4o
- Judge harness: unmodified upstream LongMemEval judge

---

## Caveats

This result should be interpreted as a strong production no-oracle configuration, not as a clean ablation against v1.

Compared with v1, v3 changes multiple factors:

1. answer model,
2. answer prompts,
3. routing method,
4. retrieval surface,
5. strategy taxonomy.

Therefore, the improvement from **89.0% oracle** to **92.0% no-oracle** should not be attributed to routing alone. The result demonstrates that the full v3 production configuration performs strongly without privileged labels.

Additional caveats:

- One non-JSON answer was counted as incorrect.
- Strategy agreement is measured post hoc and does not imply use of oracle labels.
- Session-level retrieval success does not guarantee complete evidence utilization.
- Preference-style questions remain the clearest weakness.

---

## Reproduction

```bash
# Answer phase
LME_STRATEGY_PROMPTS=lme_strategy_frontier.yaml \
python pipeline/05_answer.py \
  --backend batch \
  --batch-provider anthropic \
  --model claude-opus-4-6 \
  --strategy-route \
  --leg-tag combined-cf \
  --answerer-tag opus46@frontier-strategy \
  --max-tokens 1024
```

```bash
# Judge phase
python pipeline/06_judge.py \
  --backend batch \
  --top-k 10 \
  --answerer-model opus46@frontier-strategy
```

---

## Conclusion

The v3 production no-oracle configuration achieves **92.0%** on LongMemEval-S, outperforming the earlier v1 oracle-reported score of **89.0%**. The result is driven by a stronger answerer, strategy-specific prompting, inferred runtime routing, and high-recall retrieval.

The main remaining opportunity is preference-style reasoning, where `single-session-preference` and `PROSE` examples underperform relative to the rest of the benchmark.