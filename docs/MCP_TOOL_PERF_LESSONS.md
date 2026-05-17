# MCP tool perf — diagnose the tool shape, not the prompt

> Lessons from the 2026-05-17 curate-memory perf fix. Companion to memory
> `4090f663-b3ca-4298-a80e-9f86b22233ef`. Sister doc:
> `docs/MEMORY_CORE_MODULARIZATION_LESSONS.md` (refactor-side discipline).

## The diagnostic rule

When an MCP-driven agent (curation, dedup, audit, anything that surveys
the store and then acts on it) runs slowly, **first check what each tool
actually returns.** If an agent's tool-call count is ~10–30× higher than
its declared cap, the agent is almost certainly working around a tool
that returns a count/status string instead of the structured data the
agent needs to act on. **Fix is at the impl, not the prompt.**

Symptoms that point at tool-shape (vs prompt-tuning):

- Agent's `tool_uses` count balloons past its stated cap.
- Agent reports "fell back to direct sqlite to enumerate X" or "tool
  returned only a count."
- Survey/plan phase dwarfs the apply phase.
- Slowness persists across multiple agent runs even after prompt
  tightening.

If you tighten the agent prompt three times and the survey is still slow,
stop tuning the prompt and read the agent's tool-output verbatim.

## The 2026-05-17 case — full timing breakdown

The `curate-memory` agent had been taking 7–10 minutes per dedup pass.
We applied two fixes in sequence and measured each.

| Run                                | Survey   | Apply                | Tool calls | What changed |
|------------------------------------|----------|----------------------|------------|--------------|
| Baseline                           | ~10 min  | ~10 min (178 deletes) | 191        | single-id `memory_delete` loop; `memory_dedup` returned `"Found N groups."` string |
| After `memory_delete_bulk`         | 7.6 min  | 1 sec (14 deletes)   | 33         | bulk-delete tool shipped; dedup still string-only |
| After agent-cap tightening only    | 2.6 min  | n/a (manual apply)   | 14         | tighter PLAN-mode caps; dedup still string-only (agent stopped at cap, didn't actually delete) |
| After structured `memory_dedup`    | **42 sec** | **4.4 sec (17 deletes)** | **10**     | both fixes live, sqlite fallback gone |

**End-to-end: ~14× on the survey, ~135× on the apply. Tool calls 191 → 10.**

Note that the agent-prompt change alone (run #3) cut survey time to 2.6
min — better than baseline, but the *only* reason was that the agent hit
its tighter cap and gave up. It wasn't actually finding duplicates; it
was just failing faster. The real fix needed the tool-shape change.

## The two fixes — both shipped on `main`

### Commit `249b4b2` — `memory_delete_bulk` MCP tool

New `memory_delete_bulk_impl(ids, hard=False)` in `bin/memory_core.py`.

- One transaction per 500-id chunk.
- Batched `IN (?,?,...)` across `memory_items`, `memory_embeddings`,
  `memory_relationships`, `chroma_sync_queue`.
- Per-id `_record_history` audit rows preserved (matches single-id
  `memory_delete_impl` exactly).
- Input dedup: same id passed twice is processed once.
- Returns `{succeeded, not_found, mode}` instead of N text lines.

Destructive-gated via `ToolSpec(default_allowed=False)` in
`bin/mcp_tool_catalog.py` — same gate as the single-id `memory_delete`,
only exposed when `MCP_PROXY_ALLOW_DESTRUCTIVE` is set on the proxy.

Effect: a 178-id delete drops from ~178 MCP round-trips (~3s each =
~10 min) to one round-trip plus a single batched SQL transaction
(~4 sec).

### Commit `92ab623` — structured `memory_dedup` return + tighter `curate-memory` survey

Two coupled changes.

**`memory_dedup_impl`** in `bin/memory_maintenance.py` now returns:

```python
{
  "count": <int total groups found>,
  "groups": [
    {"a": <id>, "b": <id>, "title_a": <str>, "title_b": <str>, "score": <float>},
    ...
  ],
  "threshold": <float>,
  "scanned": <int rows scanned>,
  "applied": <bool>,
}
```

…plus a new `limit` knob so the agent can cap returned groups on stores
with many duplicates (`count` still reflects the true total).

Prior to this change, `memory_dedup_impl` returned the literal string
`"Found N duplicate groups."` with no IDs. The curate-memory agent had
no choice but to fall back to direct sqlite queries to enumerate which
pairs those N groups contained — burning 30+ tool calls per run.

**`agents/curate-memory.md`** was simultaneously tightened:

- PLAN-mode caps: `memory_search ≤ 2`, `memory_dedup ≤ 2`,
  direct-sqlite-Bash ≤ 2, **total ≤ 12** (was 25, hit was 33).
- Wall-clock budget: **2 min** (was 5, hit was 7.6).
- New "Direct-sqlite fallback policy" section: explicitly states when
  sqlite is OK (MCP errors, fields MCP doesn't expose, aggregates) and
  forbids the old enumeration workaround.
- APPLY-mode body: DELETE arrays > 5 ids → one `memory_delete_bulk`
  call, not a loop.
- PLAN-mode body rewritten as a tight 8-step script with heartbeats at
  known checkpoints.

## Generalizable rules

### 1. MCP tool returns are an API surface

When an LLM-driven agent has to parse a string to act, you've forced it
into prose-handling — slow and unreliable. Structured dicts/lists with
stable keys cost nothing extra to return and unblock batched action.

If a tool returns "Found 21 groups." think of it as the same bug as a
SQL function returning a count when the caller needs the rows: the
caller will go to the next-best data source (in our case, raw sqlite
queries via Bash) and your tool stops being the canonical path.

### 2. Agent fallback paths reveal upstream-tool gaps

When an agent's prompt says "if the MCP tool can't tell you X, fall
back to sqlite," and the agent reliably uses that fallback, the tool
needs to expose X. Read your own agent prompts for
`if ... fall back ...` patterns and treat each as a tool-spec wishlist
item.

The 2026-05-17 case had this exact text in `agents/curate-memory.md`:

> "If an MCP tool fails with 'not found,' fall back to direct sqlite3
> via Bash against the resolved DB path — but log this fallback in your
> report so the user knows the canonical path errored."

The agent was reliably hitting this path every run. That was the signal
to fix the tool, not the prompt.

### 3. Bulk variants for any tool an agent calls in a loop

Single-id `memory_delete` is fine for one-offs; for curation passes the
agent will (correctly) try to delete N items and each round-trip is
~3 sec. Either ship a bulk version or pre-stage the work. Same applies
to `memory_update`, `memory_link`, `memory_write` if you're seeing those
in agent loops.

Bulk variants don't replace single-id tools — both have legitimate
uses. A bulk variant exists so the agent has the choice; agents tend
to pick whichever is documented in the prompt, so update the prompt
too (see commit `92ab623` for the curate-memory agent's "DELETE > 5
ids → bulk" rule).

### 4. Restart the MCP server between tool-shape changes

The proxy builds the tool catalog once per process (`_catalog_built`
flag in `bin/mcp_proxy.py:397`). After `git push`, `/mcp restart` on
the client is required before the new shape reaches any agent.

The 2026-05-17 session needed two restart cycles, both necessary —
the first restart predated the relevant commit. **Verify the new tool
schema is actually live before re-running the slow agent:** use
`ToolSearch` to inspect the schema, look for the new params/description.
If you see the old shape, the server is stale.

### 5. Sub-2-minute curation passes are achievable

With the right tool shapes, a curation pass on a 1,000-item store can
be fully surveyed in ~10 tool calls (one `memory_dedup`, one optional
`memory_search` for context, a handful of heartbeats) and applied in
1 bulk call. If your curation runs are 5+ min there's a tool-shape
bug to find — measure tool_uses count, look for sqlite-fallback log
lines in the agent's output.

### 6. Measure run-by-run, attribute speedup to the right fix

The 2026-05-17 fix landed in two commits in sequence. Without
measuring each step, we'd have credited the apply-phase speedup to
the wrong commit. The timing table above shows commit `249b4b2`
fixed apply (1 sec vs ~10 min) but left survey at 7.6 min. Commit
`92ab623` is what dropped survey to 42 sec. Both were necessary.

When shipping perf fixes in sequence, commit each one separately and
measure between commits so the attribution is sound.

## Cross-references

- Implementation:
  - `bin/memory_core.py` — `memory_delete_bulk_impl` definition (after `memory_delete_impl`)
  - `bin/memory_maintenance.py` — `memory_dedup_impl` with structured return
  - `bin/mcp_tool_catalog.py` — both ToolSpec registrations
  - `bin/mcp_proxy.py:397` — `_catalog_built` cache (why restart is needed)
- Agent:
  - `agents/curate-memory.md` — updated PLAN / APPLY scripts and tool-call caps
- Commits: `249b4b2` (bulk delete), `92ab623` (structured dedup + agent caps)
- Memory `4090f663-b3ca-4298-a80e-9f86b22233ef` — same lessons as a
  searchable reference memory
- `docs/MEMORY_CORE_MODULARIZATION_LESSONS.md` — sister doc for refactor-side discipline
