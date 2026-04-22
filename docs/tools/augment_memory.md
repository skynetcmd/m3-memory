---
tool: bin/augment_memory.py
sha1: 905e2ea3dd23
mtime_utc: 2026-04-22T01:03:02.022007+00:00
generated_utc: 2026-04-22T01:22:54.449851+00:00
private: false
---

# bin/augment_memory.py

## Purpose

Offline post-ingest augmentation utilities for memory_items.

Two independent operations that improve retrieval quality on an already-
ingested DB without re-running the full ingest pipeline:

  link-adjacent
      Create ``related`` relationship edges between consecutive turns
      (turn N -> turn N+1) within each conversation. Graph expansion then
      bridges the gap between an assistant echo and the user statement
      that prompted it, which helps user-fact retrieval even without the
      intent-routing predecessor-pull being enabled.

  enrich-titles
      Use the SLM (``slm_intent.extract_entities``) to prefix user-turn
      titles with 1-3 pithy entities. "Sparky, Golden Retriever | ..."
      makes BM25 hit on the proper noun even when the body text uses a
      pronoun. Requires ``M3_SLM_CLASSIFIER=1`` and the entity_extract
      profile — off otherwise.

Both operations are idempotent-ish: link-adjacent uses memory_link_impl
which dedupes on (from_id, to_id, relationship_type); enrich-titles only
rewrites a title if the extracted prefix isn't already present.

Usage:
    python bin/augment_memory.py link-adjacent --database memory/x.db
    python bin/augment_memory.py enrich-titles --database memory/x.db --limit 500
    python bin/augment_memory.py all --database memory/x.db  # both in sequence

## Entry points

- `if __name__ == "__main__"` guard

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--user-id` | Restrict to a single user_id | `` | Processes all users' turns | str | Restricts processing to turns belonging to user_id only |
| `--limit` | Max candidate turns to scan | `10000` | Scans up to 10,000 candidate turns | int | Limits scan to first N candidate turns |
| `--user-id` | Restrict to a single user_id | `` | Processes all users' turns | str | Restricts processing to turns belonging to user_id only |
| `--limit` | Max rows to enrich in this run | `200` | Enriches up to 200 user turns | int | Limits enrichment to first N user turns |
| `--user-id` | Restrict to a single user_id | `` | Processes all users' turns | str | Restricts processing to turns belonging to user_id only |
| `--limit` | Limit for the enrich-titles phase | `200` | Enriches up to 200 user turns | int | Limits enrichment to first N user turns |
| `--database` | SQLite database path. Env: M3_DATABASE. Default: memory/agent_memory.db. | None | Falls back to M3_DATABASE env then memory/agent_memory.db. | str | Routes this run against PATH for all DB reads/writes. |

## Environment variables read

_(none detected)_

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (add_database_arg, resolve_db_path)`
- `memory_core (_db, memory_link_impl)`
- `slm_intent (extract_entities)`

## Calls OUT (external side-channels)

_(no subprocess / http / sqlite calls detected)_

## Notable external imports

_(only stdlib)_

## File dependencies (repo paths referenced)

_(none detected)_

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
