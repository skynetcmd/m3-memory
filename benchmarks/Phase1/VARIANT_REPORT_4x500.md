# LOCOMO Phase 1 — variant comparison

Variants reported: baseline, heuristic_c1c4, llm_v1, llm_only

- **baseline** — n=500, run_dir=audit_20260417_175252
- **heuristic_c1c4** — n=500, run_dir=audit_20260417_182610
- **llm_v1** — n=500, run_dir=audit_20260417_185925
- **llm_only** — n=500, run_dir=audit_20260417_193233

## Overall

| metric | baseline | heuristic_c1c4 | llm_v1 | llm_only |
|---|---|---|---|---|
| any_gold_hit_rate |  86.0% |  85.6% |  86.0% |  85.6% |
| mean_r@1 |   5.7% |   5.4% |   6.0% |   4.8% |
| mean_r@3 |   8.9% |  11.8% |  11.6% |  10.3% |
| mean_r@5 |  11.0% |  16.2% |  15.9% |  14.5% |
| mean_r@10 |  20.3% |  30.9% |  30.5% |  27.9% |
| mean_r@20 |  30.7% |  32.4% |  31.7% |  29.4% |
| mean_r@40 |  37.0% |  39.4% |  40.8% |  37.5% |

| mean_first_gold_rank | 135.8 | 132.7 | 129.6 | 133.1 |
| zero_hit_count | 70 | 72 | 70 | 72 |

## Deltas vs `baseline`

| metric | heuristic_c1c4 | llm_v1 | llm_only |
|---|---|---|---|
| any_gold_hit_rate | -0.4pp | +0.0pp | -0.4pp |
| mean_r@1 | -0.3pp | +0.3pp | -0.9pp |
| mean_r@3 | +2.9pp | +2.7pp | +1.4pp |
| mean_r@5 | +5.3pp | +5.0pp | +3.5pp |
| mean_r@10 | +10.7pp | +10.2pp | +7.6pp |
| mean_r@20 | +1.7pp | +1.1pp | -1.3pp |
| mean_r@40 | +2.4pp | +3.8pp | +0.5pp |

## Per-category any_gold_hit_rate

| category | n | baseline | heuristic_c1c4 | llm_v1 | llm_only |
|---|---|---|---|---|---|
| temporal | 91 |  82.4% |  82.4% |  82.4% |  83.5% |
| open-domain | 22 |  86.4% |  81.8% |  86.4% |  81.8% |
| multi-hop | 75 |  90.7% |  88.0% |  89.3% |  89.3% |
| single-hop | 200 |  83.5% |  85.0% |  85.0% |  84.0% |
| adversarial | 112 |  90.2% |  88.4% |  88.4% |  88.4% |

## Per-category mean_r@10

| category | n | baseline | heuristic_c1c4 | llm_v1 | llm_only |
|---|---|---|---|---|---|
| temporal | 91 |  14.3% |  23.1% |  18.7% |  17.6% |
| open-domain | 22 |  11.4% |  11.4% |  15.9% |  15.9% |
| multi-hop | 75 |   6.6% |  10.3% |   9.9% |   8.4% |
| single-hop | 200 |  22.8% |  39.5% |  38.0% |  34.5% |
| adversarial | 112 |  31.7% |  39.7% |  43.3% |  39.7% |
