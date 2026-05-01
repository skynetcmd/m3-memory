---
tool: bin/m3_enrich.py
sha1: 19552ee2aaad
mtime_utc: 2026-05-01T09:46:22.059922+00:00
generated_utc: 2026-05-01T13:05:26.836585+00:00
private: false
---

# bin/m3_enrich.py

## Purpose

m3_enrich — User-facing enrichment CLI for core memory + chatlogs.

Wraps Phase D Mastra Observer + Reflector into a single tool that any
m3 user can run on their own DBs. Supports local SLMs (LM Studio,
Ollama) and frontier cloud (Anthropic Haiku/Sonnet, OpenAI gpt-4o-mini,
Google Gemini) via YAML profiles in config/slm/.

Quick start (LM Studio + qwen3-8b loaded):
    python bin/m3_enrich.py --dry-run        # preview
    python bin/m3_enrich.py                   # enrich both DBs

Pick a different profile:
    python bin/m3_enrich.py --profile enrich_anthropic_haiku
    python bin/m3_enrich.py --profile-path /path/to/my_profile.yaml

Scope:
    --core              # only enrich agent_memory.db
    --chatlog           # only enrich agent_chatlog.db
    --include-summaries # add type='summary' rows to allowlist
    --include-notes     # add type='note' rows
    --include-types t,t # extend allowlist with custom types (additive)
    --only-use-types t,t # replace allowlist entirely (no defaults merged in)

Output:
    Observations are written as type='observation' rows under variant
    --target-variant (default: m3-observations-YYYYMMDD). Read them back
    with mcp__memory__memory_search or any retrieval call that opts into
    M3_PREFER_OBSERVATIONS=1.

Status: Phase D user-facing CLI. Pairs with bin/run_observer.py + bin/run_reflector.py.

---

## Entry points

- `def main()` (line 1232)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--profile` | f'Profile name in config/slm/. Default: {DEFAULT_PROFILE}.' | `DEFAULT_PROFILE` |  | str |  |
| `--profile-path` | Explicit YAML path. Overrides --profile when set. | None |  | str |  |
| `--reflector-profile` | Override the Reflector stage with a different profile. Defaults to --profile (same model for both stages). | None |  | str |  |
| `--core` | f"Only enrich the core memory DB (skip chatlog). Auto-broadens default type allowlist to: {','.join(DEFAULT_CORE_TYPES)}." | `False` |  | store_true |  |
| `--chatlog` | f"Only enrich the chatlog DB (skip core). Default type allowlist stays message-shaped: {','.join(DEFAULT_CHATLOG_TYPES)}." | `False` |  | store_true |  |
| `--core-db` | Explicit path to the core memory DB. | None |  | str |  |
| `--chatlog-db` | Explicit path to the chatlog DB. | None |  | str |  |
| `--target-variant` | Variant tag for emitted observations. Default: m3-observations-YYYYMMDD. | `f'm3-observations-{_today()}'` |  | str |  |
| `--source-variant` | Filter source rows by variant. '__none__' = true core memory only (variant IS NULL). A name string = single-variant scope. Default: no filter (all rows). | None |  | str |  |
| `--source-conv-list` | Path to a file listing group_keys (conversation_ids) to process. Format: newline-delimited text (with optional # comments) OR a JSON array of strings. Narrows the eligible-groups set AFTER --source-variant + type filtering — opt-in lever, no effect on default behavior. Env: M3_ENRICH_CONV_LIST. | `os.environ.get('M3_ENRICH_CONV_LIST')` |  | str |  |
| `--track-state` | Record per-group enrichment state in the enrichment_groups table (migration 028). Required for --resume / --budget-usd. Requires --source-variant. Env: M3_ENRICH_TRACK_STATE. | `os.environ.get('M3_ENRICH_TRACK_STATE', '0').lower() in ('1', 'true', 'yes')` |  | store_true |  |
| `--resume` | Skip groups already at status='success' or 'empty' for the current (source_variant, target_variant) pair. Implies --track-state. Picks up pending + failed-with-retries-left. | `False` |  | store_true |  |
| `--include-dead-letter` | Also retry groups currently at status='dead_letter'. Manual override; implies --resume. Use after fixing the underlying issue (prompt change, model upgrade, etc.). | `False` |  | store_true |  |
| `--max-attempts` | f'Per-group retry cap before promotion to dead_letter. Default {estate.DEFAULT_MAX_ATTEMPTS}. Env: M3_ENRICH_MAX_ATTEMPTS.' | `int(os.environ.get('M3_ENRICH_MAX_ATTEMPTS', estate.DEFAULT_MAX_ATTEMPTS))` |  | int |  |
| `--budget-usd` | Hard ceiling on cumulative cost_usd across this run. When tripped, drains inflight calls and exits cleanly with status='aborted'. Implies --track-state. Env: M3_ENRICH_BUDGET_USD. | `float(os.environ['M3_ENRICH_BUDGET_USD']) if os.environ.get('M3_ENRICH_BUDGET_USD') else None` |  | float |  |
| `--sample` | Process at most N groups, selected via --sample-strategy. Independent of --limit (which caps the SQL pull). | None |  | int |  |
| `--sample-strategy` | How --sample picks groups. 'first' = top-N by turn-count desc (cheapest). 'random' = uniform random. 'stratified' = balanced by turn-count quartile. Default 'first'. | `first` |  | str |  |
| `--input-max-k` | Override the per-call input cap for the SLM, in KB. Caps the total chars sent to the model at N*1024. Use to fit a smaller per-slot ctx budget when raising concurrency in the model server. Falls back to profile.input_max_chars when unset. Env: M3_ENRICH_INPUT_MAX_K. | `int(os.environ['M3_ENRICH_INPUT_MAX_K']) if os.environ.get('M3_ENRICH_INPUT_MAX_K') else None` |  | int |  |
| `--min-size-k` | --resume only: pick groups whose total source content is at least N KB. Use with --max-attempts to retry the big groups at lower concurrency. Excludes legacy rows where content_size_k is NULL. Env: M3_ENRICH_MIN_SIZE_K. | `int(os.environ['M3_ENRICH_MIN_SIZE_K']) if os.environ.get('M3_ENRICH_MIN_SIZE_K') else None` |  | int |  |
| `--max-size-k` | --resume only: pick groups whose total source content is at most N KB. Pair with --concurrency to fit per-slot ctx budget. Excludes legacy rows where content_size_k is NULL. Env: M3_ENRICH_MAX_SIZE_K. | `int(os.environ['M3_ENRICH_MAX_SIZE_K']) if os.environ.get('M3_ENRICH_MAX_SIZE_K') else None` |  | int |  |
| `--limit` | Cap conversations enriched per DB (smoke testing). | None |  | int |  |
| `--concurrency` | Concurrent SLM calls. Default 4. | `4` |  | int |  |
| `--cascade-threshold` | Abort the run after N consecutive rate-limit (429) failures within --cascade-window-s seconds. Catches upstream quota walls before the run dirties the DB with thousands of phantom failures. Default 10. | `10` |  | int |  |
| `--cascade-window-s` | Time window for the consecutive-429 cascade detector. Default 60s. Any successful call resets the counter, so isolated 429s during normal operation don't trip. | `60.0` |  | float |  |
| `--report` | Write a per-run summary report at the end of the run. Default 'auto' = docs/audits/enrich-run-<date>.md. Pass an explicit path (--report path/to/file.md) to override. Pair with --no-report to disable. | `auto` |  | str |  |
| `--no-report` | Disable the auto-generated end-of-run report. | — |  | store_const |  |
| `--include-summaries` | Add type='summary' rows to the active allowlist (extends whichever default applies; redundant under --core). | `False` |  | store_true |  |
| `--include-notes` | Add type='note' rows to the active allowlist (extends whichever default applies; redundant under --core). | `False` |  | store_true |  |
| `--include-types` | Comma-separated types to ADD to the active allowlist (extends whichever default applies). E.g. '--include-types reference,project' adds those alongside the per-DB default. | None |  | str |  |
| `--only-use-types` | Comma-separated types -- REPLACES the default allowlist entirely (e.g. '--only-use-types decision,plan' selects ONLY those, no defaults merged in). Use this when you want a precise narrow list. --include-summaries / --include-notes / --include-types still extend after replacement. | None |  | str |  |
| `--drain-queue` | Phase E2: drain pending observation_queue rows that were enqueued by the chatlog auto-enrich hook (M3_AUTO_ENRICH=1). Single-shot, returns when the queue is empty. Use in cron / scheduled task for continuous enrichment. | `False` |  | store_true |  |
| `--drain-batch` | Max queue rows to process per --drain-queue invocation. Default 100 (a few minutes of work for typical convs). | `100` |  | int |  |
| `--no-reflect` | Skip the Reflector merge/supersede pass. | `False` |  | store_true |  |
| `--reflector-threshold` | Min observations per (user,conv) before Reflector fires. Default 50. | `50` |  | int |  |
| `--dry-run` | Preview what would happen without writing. | `False` |  | store_true |  |
| `--skip-preflight` | Skip endpoint-smoke and DB backup. Power-user only. | `False` |  | store_true |  |
| `--yes`, `-y` | Skip the interactive confirm prompt. | `False` |  | store_true |  |

---

## Environment variables read

- `M3_ENRICH_BUDGET_USD`
- `M3_ENRICH_CONV_LIST`
- `M3_ENRICH_INPUT_MAX_K`
- `M3_ENRICH_MAX_ATTEMPTS`
- `M3_ENRICH_MAX_SIZE_K`
- `M3_ENRICH_MIN_SIZE_K`
- `M3_ENRICH_PROFILE`
- `M3_ENRICH_TRACK_STATE`

---

## Calls INTO this repo (intra-repo imports)

- `auth_utils (get_api_key)`
- `enrichment_state`
- `run_observer`
- `run_reflector`
- `slm_intent (Profile, _parse_profile, load_profile)`
- `slm_intent (invalidate_cache)`
- `unified_ai (async_client_for_profile)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `f'file:{db_path}?mode=ro'`` (line 284)
- `sqlite3.connect()  → `f'file:{db_path}?mode=ro'`` (line 842)
- `sqlite3.connect()  → `f'file:{db_path}?mode=ro'`` (line 960)
- `sqlite3.connect()  → `str(db_path)`` (line 142)
- `sqlite3.connect()  → `str(db_path)`` (line 591)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `025_observation_queue.up.sql`
- `agent_chatlog.db`
- `agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
