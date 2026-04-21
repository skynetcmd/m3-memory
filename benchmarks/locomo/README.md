# LoCoMo Benchmark

> Benchmark harnesses are not part of the published package. They live in `benchmarks/` and require a repository checkout to run — see [CONTRIBUTING.md](../../CONTRIBUTING.md) for reproduction steps. The harness is not shipped on PyPI.

M3 Memory's retrieval audit against [LoCoMo](https://snap-stanford.github.io/locomo/) — the long-term conversational memory benchmark from Maharana et al., 2024. This suite is a **retrieval-only audit**: it measures whether the gold evidence turn appears in the top-k retrieved pool, not whether the downstream answerer uses it correctly.

See [`PLAN.md`](./PLAN.md) for the full methodology, variant presets, and reproduction commands.

## Quick start

```bash
# Baseline audit (500 questions across conv-26, conv-30, conv-41, conv-42)
python benchmarks/locomo/retrieval_audit.py --limit 500

# Compare two runs
python benchmarks/locomo/compare_runs.py --a <baseline_dir> --b <candidate_dir>

# Variant re-ingest
python benchmarks/locomo/reingest.py --variant <preset>
```

Artifacts land in `benchmarks/locomo/runs/audit_<timestamp>/`. Variant presets are defined in `reingest.py::VARIANT_PRESETS`.
