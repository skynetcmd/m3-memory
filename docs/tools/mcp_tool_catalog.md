---
tool: bin/mcp_tool_catalog.py
sha1: 8ad93772d245
mtime_utc: 2026-07-19T03:04:59.603522+00:00
generated_utc: 2026-07-19T19:29:22.557899+00:00
private: false
---

# bin/mcp_tool_catalog.py

## Purpose

mcp_tool_catalog.py — single source of truth for the m3-memory MCP tool catalog.

Imported by:
  - bin/memory_bridge.py (FastMCP stdio server — registers each spec via @mcp.tool())
  - examples/multi-agent-team/dispatch.py (orchestrator-side dispatch loop)

Zero FastMCP dependency. Pure Python + memory_core + memory_sync + memory_maintenance.
Never import this module from those modules — that would create a cycle.

Mutation-safety invariant (do not regress): mutating memory tools
(memory_delete, memory_supersede) require the FULL UUID for their target id —
a prefix is rejected via _is_full_uuid in their validators. Read tools
(memory_get) accept an 8-char prefix for convenience, but an ambiguous prefix
on a mutation could close/delete the wrong memory irreversibly. This asymmetry
is intentional; keep the validators and the "full UUID required" wording in the
tool descriptions so it survives doc-inventory regeneration. Also note:
memory_supersede is non-destructive and creates a NEW successor each call — it
is an update primitive, not a delete; do not chain it to "clean up" clutter.

── Module structure (catalog/ subpackage split) ──────────────────────────────
This module is the AGGREGATOR + INJECTION point. The actual ToolSpec support
code and the 108 ToolSpec entries live in the bin/catalog/ subpackage:
  - catalog.lazy        — LazyImpl / LazyModuleProxy
  - catalog.spec         — ToolSpec dataclass + validation constants
  - catalog.validators   — per-tool argument validators
  - catalog.dispatch      — execute_tool(_structured), timeout machinery,
                            the m3_call/m3_index dispatcher, and the inline
                            impl wrappers (_conversation_search_impl,
                            _memory_verify_impl)
  - catalog.tools_<domain> — one module per domain (admin, memory, chatlog,
                            conversations, agent, tasks, entity, diagnostics,
                            files), each exporting a flat `TOOLS` list.
This module re-imports everything the support modules define (so
`mcp_tool_catalog.ToolSpec`, `.execute_tool`, `.m3_call_impl`,
`.LazyModuleProxy`, all validators, etc. still resolve for external
importers), concatenates the 9 domain TOOLS lists into ONE flat `TOOLS` list,
then runs the `database` + `timeout` parameter injection over the aggregated
list (order does not matter for injection — every spec is mutated the same
way — but injection must run AFTER aggregation since it walks the full list),
and finally rebuilds `_BY_NAME`.

---

## Entry points

_(no conventional entry point detected)_

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (active_database)`
- `tool_loader`

---

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

---

## Notable external imports

- `catalog.dispatch (_DEFAULT_TOOL_TIMEOUT, _DESTRUCTIVE_ALLOWED, _DISPATCH_EXCLUDE, ToolTimeout, _conversation_search_impl, _did_you_mean, _dispatch_one, _memory_verify_impl, _pop_database, _resolve_tool_timeout, _run_impl_bounded, _spec_by_name, _tool_arg_rows, default_allowlist, execute_tool, execute_tool_structured, get_tool, m3_call_impl, m3_index_impl, validate_args)`
- `catalog.lazy (LazyImpl, LazyModuleProxy)`
- `catalog.spec (_UUID_RE, MAX_CONTENT_SIZE, MAX_K, MAX_QUERY_LENGTH, VALID_ENTITY_PREDICATES, VALID_ENTITY_TYPES, VALID_MEMORY_TYPES, ToolSpec, _is_full_uuid)`
- `catalog.tools_admin`
- `catalog.tools_agent`
- `catalog.tools_chatlog`
- `catalog.tools_conversations`
- `catalog.tools_diagnostics`
- `catalog.tools_entity`
- `catalog.tools_files`
- `catalog.tools_memory`
- `catalog.tools_tasks`
- `catalog.validators (_gdpr_user_id_validator, _memory_delete_validator, _memory_search_gated_validator, _memory_search_scored_validator, _memory_search_validator, _memory_set_retention_validator, _memory_suggest_validator, _memory_supersede_validator, _memory_update_validator, _memory_write_validator, _variant_gate)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
