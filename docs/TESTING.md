# Testing

All the test surfaces in this repo — unit tests, end-to-end harnesses,
benchmarks, and CI — in one place. Use this to pick the right thing to run
for the change you just made.

## Quick start — which test should I run?

| Change I made | Run this |
|---|---|
| Anything touched by CI (ruff + mypy + pytest) | `python -m pytest tests/` |
| Memory core, MCP tool schemas, dispatch logic | `python bin/test_memory_bridge.py` |
| Chatlog subsystem (ingest / queue / redact / status) | `python -m pytest tests/test_chatlog_*.py` |
| `bin/slm_intent.py` or the SLM profiles | `python -m pytest tests/test_slm_intent.py` |
| MCP proxy dispatch | `python -m unittest bin.test_mcp_proxy_unit` |
| Retrieval quality on a real dataset | `python benchmarks/longmemeval/bench_longmemeval.py` |

Full test inventory and what each exercises follows.

## Layout

Three distinct test surfaces, each with different run semantics:

```
tests/                        # pytest — CI target, no external deps
bin/test_*.py                 # standalone e2e harnesses (print PASS/FAIL)
benchmarks/                   # retrieval quality evaluation on real datasets
```

`pyproject.toml` pins `testpaths = ["tests"]`, so `pytest` on the repo root
only runs the `tests/` suite. The `bin/test_*.py` scripts are invoked
directly (`python bin/test_foo.py`) and exit with a summary. The benchmark
harnesses are evaluation drivers, not pass/fail gates — they produce
hypotheses, judge scores, and CSV/JSONL artifacts for offline analysis.

### CI wiring

`.github/workflows/ci.yml` runs three jobs on every push + PR to `main`:

1. **Lint (Ruff)** — `ruff check bin/ memory/`
2. **Type check (Mypy)** — `mypy bin/ --ignore-missing-imports`
3. **Test** — matrix of `{ubuntu, macos, windows} × {3.11, 3.12}`, each
   runs `pytest tests/`

Neither the `bin/test_*.py` harnesses nor the benchmarks run in CI (they
need LM Studio, a populated DB, or a real embedding endpoint).

## 1. pytest suite (`tests/`)

**Total**: 152 passed / 2 skipped across 18 files (as of 2026-04-21).
**Requirements**: none — all DB and LLM access is mocked or uses `tmp_path`.

| File | Tests | What it covers |
|---|---|---|
| `test_auth_utils.py` | 3 | Master-key retrieval, Fernet encrypt/decrypt, device-salt persistence |
| `test_chatlog_config.py` | 11 | Config resolution hierarchy (env > ContextVar > file > default), legacy-mode deprecation, caching |
| `test_chatlog_cost_report.py` | 4 | Cost aggregation by provider/model, token counting, null-cost exclusion |
| `test_chatlog_ingest_formats.py` | 10 | Claude Code + Gemini CLI schema parsing, provider inference, ingest idempotency |
| `test_chatlog_migrations.py` | 8 | Schema bootstrap + rollback, index creation, FTS table, triggers |
| `test_chatlog_perf.py` | 5 | Enqueue throughput, flush latency, batch-write latency, metadata overhead |
| `test_chatlog_redaction.py` | 13 | API-key / JWT / AWS / GitHub token scrubbing, PII, custom regex, compile cache |
| `test_chatlog_roundtrip.py` | 7 | Write → flush → search, conversation listing, metadata preservation |
| `test_chatlog_status.py` | 9 | Status JSON schema, row counts, queue depth, spill info, cold-start perf |
| `test_chatlog_status_line.py` | 9 | One-line status indicators, queue/spill warnings, severity ordering |
| `test_chatlog_write_queue.py` | 12 | Item validation, bulk queueing, spill file creation, cost fields |
| `test_content_safety.py` | 2 | Malicious-pattern detection (eval / exec / SQLi) + benign-content allowlist |
| `test_embedding_utils.py` | 3 | Vector pack/unpack, cosine similarity, batch cosine with heterogeneous dims |
| `test_llm_failover.py` | 3 | Model-size parsing, embed-model discovery, LLM selection heuristic |
| `test_pg_sync_fk_safety.py` | 3 | FK-safe embedding sync: parent presence, orphan deferral |
| `test_recency_bonus.py` | 8 | Recency-bonus scoring, date interpolation, undated handling, stable tie-break |
| `test_slm_intent.py` | 11 | Gate off → None, profile loader, search-dir stacking, label matching |
| `test_task_tombstones.py` | 7 | Soft/hard delete, visibility, tombstone persistence, update rejection |

### Running

```bash
# Full suite
python -m pytest tests/

# Single file with verbose output
python -m pytest tests/test_slm_intent.py -v

# Exclude the two slow tests
python -m pytest tests/ -m "not slow"

# Against an isolated DB (see bin/setup_test_db.py)
python bin/setup_test_db.py --database memory/_test.db --force
M3_DATABASE=memory/_test.db python -m pytest tests/
```

## 2. End-to-end harnesses (`bin/test_*.py`)

These are self-contained scripts that print a `✅/❌` summary. They exist
for scenarios that need a real live DB, a running MCP server, or LM Studio
on localhost — things pytest would have to mock extensively.

All honor `M3_DATABASE` so you can point them at a scratch DB:

```bash
python bin/setup_test_db.py --database memory/_test.db --force
M3_DATABASE=memory/_test.db python bin/test_memory_bridge.py
```

| File | Invocation | Count | Requires | Covers |
|---|---|---|---|---|
| `test_memory_bridge.py` | `python bin/test_memory_bridge.py` | 193 checks across 47 scenarios | LM Studio (skips embedding tests if offline); DB via `M3_DATABASE` | All 38+ MCP tools: memory write/get/search/update/delete, conversations, tasks, handoffs, inbox, agent registry, notifications, GDPR, sync |
| `test_debug_agent.py` | `python bin/test_debug_agent.py` | ~25 checks | LM Studio (graceful skip); DB via `M3_DATABASE` | Debug agent bridge: thermal, LLM routing, debug_analyze / bisect / trace / correlate / history / report |
| `test_mcp_proxy.py` | `python bin/test_mcp_proxy.py` | 4 scenarios, ~10 checks | MCP proxy running on :9000, `ANTHROPIC_API_KEY`, DB | Proxy end-to-end: `/health`, Claude + Gemini routing, aider subprocess, activity_logs delta |
| `test_mcp_proxy_unit.py` | `python -m unittest bin.test_mcp_proxy_unit` (or pytest) | ~15 methods | none | Proxy internals: tool-list merging, destructive-tool gating, `inject_agent_id` enforcement |
| `test_bulk_parity.py` | `python bin/test_bulk_parity.py` | ~15 checks | SQLite (mocked schema) | Bulk vs single write parity: enrichment, variant isolation, contradiction detection |
| `test_embedding_logic.py` | `python bin/test_embedding_logic.py` | 2 ops | LM Studio, `LM_API_TOKEN` | Embed path smoke: token resolution, `_embed()` round-trip |
| `test_keychain.py` | `python bin/test_keychain.py` | 3 lookups | OS keychain or env vars | `auth_utils.get_api_key()` across env / keyring / platform-native stores |
| `test_knowledge.py` | `python bin/test_knowledge.py` | 2 test classes | LM Studio (optional for search) | Knowledge-helpers CRUD: add / list / delete / search |
| `test_unified_router.py` | `python bin/test_unified_router.py` | 3 payloads | LM Studio on :1234, `LM_API_TOKEN`, optional `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | OpenAI-compatible router: local dispatch + cloud fallback (Claude / Gemini) |
| `test_mission_control.py` | `python bin/test_mission_control.py` | ~18 checks | LM Studio (skip if offline), nvidia-smi (skip if no GPU), network (skip if unreachable) | Status dashboard: platform detect, paths, KV stats, GPU / VRAM, ping, single render pass |

### Routing test output to a scratch DB

The three harnesses that take over the live DB (`test_memory_bridge.py`,
`test_debug_agent.py`, `test_mcp_proxy.py`) resolve `DB_PATH` at import
time via `m3_sdk.resolve_db_path(None)`, so setting `M3_DATABASE` before
invocation isolates them. Typical flow:

```bash
python bin/setup_test_db.py --database memory/_test.db --force
M3_DATABASE=memory/_test.db python bin/test_memory_bridge.py
M3_DATABASE=memory/_test.db python bin/test_debug_agent.py
```

See [CLI_REFERENCE.md](CLI_REFERENCE.md#running-tests-against-an-isolated-db)
for details.

## 3. Benchmark harnesses (`benchmarks/`)

These measure retrieval quality and ingest cost against real datasets. They
are **not** pass/fail gates — they produce artifacts in
`.scratch/<run>/` or `benchmarks/locomo/runs/<run>/` that downstream
analysis utilities consume. Datasets (`data/longmemeval/`, `data/locomo/`)
are `.gitignore`'d and fetched out-of-band.

### Bench drivers (end-to-end run)

| File | What it measures | Requires |
|---|---|---|
| `benchmarks/longmemeval/bench_longmemeval.py` | LongMemEval QA accuracy across temporal, factual, and update-tracking question types. Ingests long-session conversations, retrieves top-K, generates + judges. | `data/longmemeval/longmemeval_s_cleaned.json`; `LLM_ENDPOINTS_CSV` (default `http://localhost:8081/v1`); judge model via `--judge-model` or `EVAL_JUDGE_MODEL` |
| `benchmarks/locomo/bench_locomo.py` | LOCOMO dialog-QA: multi-hop, temporal, open-domain, single-hop, and adversarial categories. Ingest → retrieve → generate → judge against gold. | `data/locomo/locomo10.json`; `LLM_ENDPOINTS_CSV` (default `http://localhost:1234/v1`); embedding server; judge model |

Both honor `M3_DATABASE` so you can point them at a benchmark-only DB
without touching production. See the top of each script's docstring for
full flag listings.

### Sub-utilities (consume prior run artifacts)

These analyze the output of a bench driver — not drivers themselves. Run
`bench_locomo.py` or `retrieval_audit.py` first, then invoke these against
the resulting `retrieval_trace.jsonl` or `summary.json`.

| File | What it does | Requires |
|---|---|---|
| `benchmarks/locomo/retrieval_audit.py` | Recall@K audit without answer generation / judging. Outputs `retrieval_trace.jsonl` ranking gold evidence dia_ids. | `data/locomo/locomo10.json`; embedding server |
| `benchmarks/locomo/analyze_handoff.py` | Where do gold hits land in ranking? Precision@K, zero-hit rate, role distribution, session-date coverage. | Prior `retrieval_audit.py` run (reads `retrieval_trace.jsonl`) |
| `benchmarks/locomo/analyze_prompt.py` | Prompt anatomy: size, whether gold references survive rendering, character offsets, per-category "waste" metrics. | Prior `retrieval_audit.py` run |
| `benchmarks/locomo/compare_runs.py` | Side-by-side delta between two audit runs: recall@K, mean-first-gold-rank, per-category. | Two prior run directories under `benchmarks/locomo/runs/` |
| `benchmarks/locomo/reingest.py` | Re-ingest LOCOMO with explicit variant configs (baseline / heuristic_c1c4 / llm_v1 / llm_only). Reuses in-process LLM caches across variants. | `data/locomo/locomo10.json`; embedding server |
| `benchmarks/locomo/probe_ingest_cost.py` | Profile ingest cost across variants: wall-clock, CPU (Python + LM Studio), LLM calls + tokens, embedding calls + chars, rows written. | `data/locomo/locomo10.json`; LM Studio; psutil |
| `benchmarks/locomo/probe_issues.py` | Structural audit of dataset + ingest: role distribution, gold-evidence format, zero-hit root causes. | `data/locomo/locomo10.json`; direct SQLite access |
| `benchmarks/locomo/stamp_variants_from_chainlog.py` | Retrofit the `variant` field into existing `summary.json` files by matching audit timestamps to a chain-runner log. | `.chain.log`; `benchmarks/locomo/runs/` |
| `benchmarks/locomo/join_variant_reports.py` | Aggregate latest runs across multiple variants into a markdown comparison report: hit rate, recall@K, per-category. | Multiple `benchmarks/locomo/runs/` directories |

## 4. Running the full test ladder

When merging a large change and you want maximum coverage:

```bash
# 1. CI-equivalent (fast, no external deps)
ruff check bin/ memory/
mypy bin/ --ignore-missing-imports
python -m pytest tests/

# 2. Bridge + subsystem e2e (needs LM Studio + live DB)
python bin/setup_test_db.py --database memory/_test.db --force
M3_DATABASE=memory/_test.db python bin/test_memory_bridge.py
M3_DATABASE=memory/_test.db python bin/test_debug_agent.py
python -m unittest bin.test_mcp_proxy_unit

# 3. Benchmark smoke (small sample so it finishes in minutes, not hours)
python benchmarks/longmemeval/bench_longmemeval.py --limit 5 --no-judge
```

## 5. Writing new tests

**Prefer `tests/` (pytest)** for anything that:
- Can mock its dependencies
- Doesn't need a running LM Studio / Ollama
- Should gate merges via CI

**Use `bin/test_*.py`** only when:
- The scenario truly needs a live MCP server or live LM Studio
- You're debugging end-to-end behavior and need to print intermediate state
- A pytest version would be drowning in mocks

**Use `benchmarks/`** only for:
- Retrieval quality measurement against a real dataset
- Ingest-cost profiling
- Anything that produces artifacts for offline analysis

### Writing a new pytest file

Place under `tests/test_<subject>.py`. Follow the `test_slm_intent.py`
pattern:

- `conftest.py` provides a shared `_isolate_chatlog_env` fixture
- Use `tmp_path` and `monkeypatch` for all file / env isolation
- Mark slow tests with `@pytest.mark.slow`
- No hard-coded paths — everything relative to `tmp_path`

### Adding a gate-controlled module

If your feature is env-gated (like `M3_SLM_CLASSIFIER`), test both states:

```python
def test_feature_returns_none_when_gate_off(monkeypatch):
    monkeypatch.delenv("M3_FEATURE_GATE", raising=False)
    assert my_feature("input") is None

def test_feature_runs_when_gate_on(monkeypatch, tmp_path):
    monkeypatch.setenv("M3_FEATURE_GATE", "1")
    # ... positive path ...
```

The `test_slm_intent.py` module demonstrates this end-to-end.

## 6. Related docs

- [`docs/CLI_REFERENCE.md`](CLI_REFERENCE.md) — every DB-aware CLI, including
  `bin/setup_test_db.py` for isolated test DBs
- [`docs/CHATLOG.md`](CHATLOG.md) — architecture of the chatlog subsystem
  exercised by ~60 of the pytest cases
- [`docs/SLM_INTENT.md`](SLM_INTENT.md) — SLM classifier subsystem (tested
  by `tests/test_slm_intent.py`)
- [`docs/CHANGELOG_2026.md`](CHANGELOG_2026.md) — dated record of which test
  counts landed with which refactor
