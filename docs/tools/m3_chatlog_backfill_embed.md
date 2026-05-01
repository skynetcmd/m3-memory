---
tool: bin/m3_chatlog_backfill_embed.py
sha1: 57735152a3b3
mtime_utc: 2026-04-28T02:30:35.391295+00:00
generated_utc: 2026-05-01T13:05:26.814340+00:00
private: false
---

# bin/m3_chatlog_backfill_embed.py

## Purpose

m3_chatlog_backfill_embed — Embed unembedded rows in core memory + chatlog.

Free-win recall fix from the 2026-04-26 chatlog analysis (memory id
37633aff): in older chatlog DBs the majority of `chat_log` rows have NO
embedding, leaving them invisible to vector search. This tool finds rows
in `memory_items` that lack a corresponding `memory_embeddings` row and
batch-embeds them using the local embedding server.

Apply to:
  - agent_memory.db (core memory) — usually mostly-embedded
  - agent_chatlog.db (chatlog) — typically has the most missing embeddings

Idempotent: if every eligible row already has an embedding, exits 0
without spending compute.

Quick start (LM Studio with text-embedding-bge-m3 loaded):
    python bin/m3_chatlog_backfill_embed.py --dry-run
    python bin/m3_chatlog_backfill_embed.py

Defaults:
  - covers both DBs in one pass (--core / --chatlog narrow)
  - skips rows with content shorter than --min-chars (default 10)
  - applies type allowlist matching m3_enrich (configurable via
    --include-types)
  - chunked batches of --batch (default 32) per embed call

---

## Entry points

- `def main()` (line 339)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--core` | Only backfill the core memory DB (skip chatlog). | `False` |  | store_true |  |
| `--chatlog` | Only backfill the chatlog DB (skip core). | `False` |  | store_true |  |
| `--core-db` | Explicit path to core memory DB. | None |  | str |  |
| `--chatlog-db` | Explicit path to chatlog DB. | None |  | str |  |
| `--include-types` | Comma-separated types to backfill. Default: chat_log,message,conversation,summary,note,observation,fact_enriched,fact. | None |  | str |  |
| `--all-types` | Backfill every memory_items type (overrides --include-types). | `False` |  | store_true |  |
| `--min-chars` | Skip rows whose content is shorter than this. Default 10. | `10` |  | int |  |
| `--batch` | Embed-call batch size. Default 32. | `32` |  | int |  |
| `--limit` | Cap rows backfilled per DB (smoke testing). | None |  | int |  |
| `--dry-run` | Preview only. | `False` |  | store_true |  |
| `--skip-backup` | Don't create a pre-run DB backup. | `False` |  | store_true |  |
| `--yes`, `-y` | Skip the confirm prompt. | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `embedding_utils (pack)`
- `memory_core`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `f'file:{db_path}?mode=ro'`` (line 206)
- `sqlite3.connect()  → `f'file:{db_path}?mode=ro'`` (line 76)
- `sqlite3.connect()  → `str(db_path)`` (line 139)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `agent_chatlog.db`
- `agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
