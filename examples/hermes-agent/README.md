# m3-memory provider for Hermes Agent

A [Hermes Agent](https://github.com/nousresearch/hermes-agent) `MemoryProvider`
plugin that uses **m3-memory** as the agent's long-term memory backend —
replacing the builtin `MEMORY.md`/`USER.md` + Honcho stack.

Patterned on hermes-agent's own `plugins/memory/mem0/` provider, so it plugs
into the same single-select memory-provider slot.

## What m3 adds over the builtin layer

Hybrid FTS5 + vector recall with MMR diversity rerank, a bitemporal model, KG
supersession (real contradiction edges, not flat dedup), and async
Observer/Reflector fact extraction — all local, no server or proxy.

## Layout

```
plugins/memory/m3/
  __init__.py     M3MemoryProvider(MemoryProvider) + register(ctx)
  m3client.py     sync facade over m3's structured catalog dispatch
  plugin.yaml     plugin manifest
test_provider_logic.py   standalone logic test (mocks the hermes-agent imports)
```

The `plugins/memory/m3/` path mirrors hermes-agent's plugin layout so the `m3/`
directory drops straight in.

## Install

1. Copy `plugins/memory/m3/` into your hermes-agent checkout at the same path
   (`plugins/memory/m3/`).
2. Put m3-memory's `bin/` on `PYTHONPATH` so `import mcp_tool_catalog` resolves
   in the Hermes process — the provider calls m3 **in-process**, no server or
   proxy needed:
   ```
   export PYTHONPATH=/path/to/m3-memory/bin:$PYTHONPATH
   ```
3. Select it via `hermes plugins` (memory providers are single-select) or
   `config.yaml`. Optional config: `M3_USER_ID`, `M3_AGENT_ID`, or
   `$HERMES_HOME/m3.json`.

## How it maps to m3

| Hermes hook | M3Client method | m3 catalog tool |
|---|---|---|
| `prefetch` / `m3_search` | `search()` | `memory_search_scored` |
| `m3_profile` | `get_all(type="user_fact")` | `memory_search_scored` (empty-query filter) |
| `m3_conclude` | `conclude()` | `memory_write` (verbatim) |
| `sync_turn` | `chatlog_write()` | `chatlog_write` (async Observer extract) |

The m3-side dependency — a `memory_search_scored` ToolSpec returning structured
`[(score, item)]` rows — ships in m3-memory's catalog (`bin/mcp_tool_catalog.py`).

## Design notes

- **Empty-query path** — `m3_profile`/`get_all` pass `query=""`;
  `memory_search_scored`'s validator accepts that (it does NOT reuse
  `memory_search`'s empty-query-rejecting validator).
- **Bench-gate parity** — `memory_search_scored` applies the same bench-data
  gate as `memory_search`, so recall never surfaces variant/bench rows.
- **Persistent event loop** — `M3Client` runs one long-lived asyncio loop on a
  dedicated thread (not `asyncio.run()` per call), preserving m3's
  connection-pool + embedder-semaphore loop affinity and the hot-path latency
  budget.

## Testing

`test_provider_logic.py` validates the provider's logic without a hermes-agent
checkout — it stubs the three hermes-agent imports (`agent.memory_provider`,
`tools.registry`, `hermes_constants`) and a fake `M3Client`:

```
python examples/hermes-agent/test_provider_logic.py
```

(21 checks: tool dispatch, top_k cap, circuit breaker, prefetch threading,
sync_turn. On Windows, set `PYTHONUTF8=1` for console output.)

Full end-to-end validation requires dropping `m3/` into a real hermes-agent
checkout and running its plugin smoke test — the provider's
`from agent.memory_provider import ...` resolves only there.
