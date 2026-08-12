---
tool: bin/memory_maintenance.py
sha1: 399365ecf308
mtime_utc: 2026-08-09T19:41:32.430027+00:00
generated_utc: 2026-08-12T00:59:01.618710+00:00
private: false
---

# bin/memory_maintenance.py

## Purpose

_(no module docstring — update the source file.)_

---

## Entry points

- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

_(no argparse arguments detected)_

---

## Environment variables read

- `M3_DISTILL_MODEL`

---

## Calls INTO this repo (intra-repo imports)

- `_task_runtime (add_log_file_arg, setup_task_runtime)`
- `agent_protocol (strip_code_fences)`
- `audit_trail (write_audit_entry)`
- `llm_failover (apply_thinking_suppression)`
- `m3_sdk (_LAST_USER_INTERACTION)`
- `memory_core`
- `memory_core (DEDUP_LIMIT, DEDUP_THRESHOLD, EMBED_DIM, _content_hash, _cosine, _db, _embed, _get_embed_client, _pack, _unpack, ctx, get_best_llm, m3_core_rs, memory_link_impl)`
- `memory_core (memory_write_impl)`
- `run_reflector (JSON_RE)`
- `slm_intent (_call_model, load_profile)`

---

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 1275)


---

## Notable external imports

- `base64`
- `httpx`
- `memory (confidence)`
- `memory (trust)`
- `memory.backends (dialect)`
- `memory.db (savepoint)`
- `memory.db (tolerant_schema)`
- `wiki.erasure (restrict_derived_on_erasure)`
- `wiki.ledger (scan_and_flush_on_erasure)`

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
