---
tool: bin/bench_longmemeval.py
sha1: cc5543f2a4e7
mtime_utc: 2026-05-31T07:42:38.486157+00:00
generated_utc: 2026-05-31T18:42:52.587396+00:00
private: true
---

# bin/bench_longmemeval.py

## Purpose

LongMemEval benchmark runner for m3-memory.

Loads the cleaned LongMemEval-S dataset, bulk-ingests every conversation turn
into m3-memory scoped by question_id (so each instance has its own isolated
haystack), then for each question retrieves the top-K most relevant turns and
asks an LLM to answer. An OpenAI judge (default gpt-4o-mini) scores the answer
using the official LongMemEval per-task prompts.

Routes embeddings through the new `memory_write_bulk_impl` / `_embed_many` path
and expects llama-server on http://localhost:8081/v1 (override with
LLM_ENDPOINTS_CSV).

Usage:
    python bin/bench_longmemeval.py                         # full 500 instances
    python bin/bench_longmemeval.py --limit 20              # subsample
    python bin/bench_longmemeval.py --skip-ingest           # reuse already-loaded DB
    python bin/bench_longmemeval.py --no-judge              # write hypotheses only
    python bin/bench_longmemeval.py --judge-only FILE       # judge an existing hyp file

Artifacts go to .scratch/longmemeval_run_<timestamp>/:
    hypotheses.jsonl   one line per question
    results.json       aggregate accuracy + per-type breakdown
    run.log            progress/errors

---

## Entry points

- `async def run()` (line 1052)
- `def main()` (line 1500)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--dataset` |  | `DEFAULT_DATASET` |  | Path |  |
| `--limit` | subsample first N instances (0 = all) | `0` |  | int |  |
| `--skip-ingest` |  | `False` |  | store_true |  |
| `--no-judge` |  | `False` |  | store_true |  |
| `--k` | top-K retrieved turns per question | `10` |  | int |  |
| `--k-reasoning` | top-K for reasoning categories (temporal-reasoning, multi-session, knowledge-update, single-session-preference). These need more context to stitch facts across sessions; k=10 starves them. Set equal to --k to disable the per-category bump. | `20` |  | int |  |
| `--recency-bias` | Score bonus added to the newest candidate and linearly interpolated to 0 for the oldest. Applied only to knowledge-update and temporal-reasoning questions — categories where 'most recent' is always the correct answer. Default 0.05 is enough to flip supersession ties without overwhelming semantic scores. Set to 0 to disable. | `0.05` |  | float |  |
| `--answer-model` |  | `claude-opus-4-6` |  | str |  |
| `--judge-model` |  | `gpt-4o` |  | str |  |
| `--answer-max-tokens` | Max output tokens for the answer model. Default 8000 fits non-thinking frontier models; bump to 16000-32000 for Claude extended thinking or o1/o3 high reasoning effort. | `ANSWER_MAX_TOKENS_DEFAULT` |  | int |  |
| `--thinking-budget` | Enable Anthropic extended thinking with this token budget (>=1024). 0 disables. Ignored for OpenAI models. When enabled the answer model runs at temperature=1.0 (Anthropic requirement). | `0` |  | int |  |
| `--reflection` | Run a Hindsight-style two-step reflection pass before the final answer. First call produces a structured TIMELINE/CONTRADICTIONS/SUPERSEDED/APPLICABLE FACTS summary; second call answers with that summary prepended. Only activates for reasoning-limited categories (temporal, multi-session, preference, knowledge-update). Mutually exclusive with --thinking-budget. | `False` |  | store_true |  |
| `--reflection-model` | Model for the reflection pre-pass (first step). Defaults to --answer-model. Set to a cheaper model (e.g. gpt-4o-mini, claude-haiku-4-5) to reduce reflection cost. | `` |  | str |  |
| `--chain-of-note` | Enable Chain-of-Note + JSON history (LongMemEval paper §5.5). Runs a per-session extraction pass that writes 'reading notes' of facts relevant to the question, then sends both the notes AND the JSON-serialized retrieved history to the final answer call. Reported as up to +10 absolute pts on oracle retrieval. Adds one extra LLM call per retrieved session per question, so expect ~2-3x answer-phase wall time and token cost. Mutually exclusive with --reflection. | `False` |  | store_true |  |
| `--chain-of-note-model` | Model for the Chain-of-Note extraction pass. Defaults to --answer-model. Set to a cheaper model (gpt-4o-mini, claude-haiku-4-5) to cut extraction cost — extraction is a structured per-chunk task that doesn't need a frontier model. | `` |  | str |  |
| `--chain-of-note-compare` | Run BOTH plain and CoN answer pipelines off the same retrieval and judge both. Primary hypotheses go to hypotheses.jsonl as usual; CoN hypotheses go to hypotheses_con.jsonl. Summary prints per-category plain-vs-con delta. Use --chain-of-note-model to set the extractor model. Ignores --chain-of-note. | `False` |  | store_true |  |
| `--ingest-concurrency` | number of instances to ingest in parallel | `4` |  | int |  |
| `--ingest-mode` | turn (default): one memory per chat turn, fine-grained retrieval. session: one memory per full session text block with [Conversation date: ...] header, matches Memento's default ingest style. Session mode gives the embedder full conversational context per vector at the cost of coarser top-k granularity. | `turn` |  | str |  |
| `--wipe-run` | Delete all bench rows tagged with this RUN_ID (the value printed at the start of every run, e.g. 'lme-20260413-194821-abc123'), then VACUUM + ANALYZE and exit. Uses idx_mi_change_agent for a single indexed delete. Run-scoped: does not touch other runs. | `` |  | str |  |
| `--wipe-all-bench` | Delete every row tagged change_agent LIKE 'bench:%%' across all runs, then VACUUM + ANALYZE and exit. Use this when you want a completely clean slate. | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`
- `memory_core`
- `memory_core (DB_PATH)`
- `memory_core (memory_write_bulk_impl, memory_search_scored_impl, _db)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `DB_PATH`` (line 104)


---

## Notable external imports

- `anthropic (Anthropic)`
- `openai (OpenAI)`

---

## File dependencies (repo paths referenced)

- `longmemeval_s_cleaned.json`
- `results.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
