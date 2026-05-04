---
tool: bin/backfill_content_hash.py
sha1: 351e966af079
mtime_utc: 2026-05-04T22:04:47.603377+00:00
generated_utc: 2026-05-04T22:24:28.983363+00:00
private: false
---

# bin/backfill_content_hash.py

## Purpose

backfill_content_hash.py — populate memory_embeddings.content_hash on legacy rows.

Why: the embed-cache lookup in memory_core._embed / _embed_many uses
`WHERE embed_model = ? AND content_hash IN (?, ...)`. Rows with NULL
content_hash are invisible to that cache — duplicate-text re-embeds
incur a fresh ~2080ms llama_encode roundtrip instead of a sub-ms hit.

Older write paths (and chatlog_embed_sweeper before commit d4b7b2c)
inserted memory_embeddings rows without populating content_hash. This
tool computes the hash from each row's source content (matching what
memory_core._content_hash would produce at write time) and UPDATEs the
embedding row in place.

Idempotent — re-running picks up only rows still NULL. Safe to run
alongside live writers (single-row UPDATEs commit per batch in WAL
mode; no schema change).

Scope decision:
  - For chat_log / message rows, the embed text is the raw content
    (chatlog sweeper uses identity transform). Hash matches.
  - For other types, memory_write_impl applies _augment_embed_text_with_anchors
    to the content + metadata before hashing. To match exactly, we'd need
    to re-augment during backfill. We skip non-chat_log/message types by
    default (--types defaults to chat_log,message) so the backfilled
    hashes match what _embed_many computes for new embeds; pass --types
    explicitly with --augment-anchors to backfill other types with the
    augmentation transform applied.

Usage:

    # Default: only chat_log + message (raw-content hashes are safe)
    python bin/backfill_content_hash.py --db memory/agent_chatlog.db

    # Smoke test 100 rows
    python bin/backfill_content_hash.py --db memory/agent_chatlog.db --limit 100

    # Dry run — show counts, write nothing
    python bin/backfill_content_hash.py --db memory/agent_chatlog.db --dry-run

    # Custom type + augmented hashing (matches inline _embed behavior for non-chatlog)
    python bin/backfill_content_hash.py --db memory/agent_memory.db \
        --type summary --type note --augment-anchors

    # Sharded across multiple invocations
    python bin/backfill_content_hash.py --db DB --id-prefix 0
    python bin/backfill_content_hash.py --db DB --id-prefix 1

---

## Entry points

- `def main()` (line 310)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--db` | f'Target DB. Default: $M3_DATABASE or {DEFAULT_DB}' | `Path(os.environ.get('M3_DATABASE', str(DEFAULT_DB)))` |  | Path |  |
| `--type` | f"Memory type to include. Repeatable. Defaults to {' + '.join(DEFAULT_TYPES)} (raw-content hashing is safe for these). For other types, pass --type explicitly with --augment-anchors so the hash matches what _embed_many would compute." | None |  | append |  |
| `--all-types` | Backfill rows of any type (no type filter). Pair with --augment-anchors so hashes match what memory_write_impl would have computed at write time. Note: rows whose metadata.temporal_anchors predates current schema will get a hash that doesn't match new writes — strictly an improvement on NULL (cache miss → cache hit on identical future text) but may not deduplicate against older inline-written embeddings of the same text. | `False` |  | store_true |  |
| `--variant` | Filter to memory_items.variant. Repeatable for OR. | `[]` |  | append |  |
| `--user-id` | Filter to one memory_items.user_id. | None |  | str |  |
| `--id-prefix` | Backfill only embedding rows whose id starts with this hex prefix. Use to shard across instances. | None |  | str |  |
| `--limit` | Stop after AT LEAST N successful updates. The check fires at batch boundaries; actual stop can overshoot by up to one batch (--batch-size). | None |  | int |  |
| `--batch-size` | f'Rows per UPDATE batch. Default: {DEFAULT_BATCH_SIZE}.' | `DEFAULT_BATCH_SIZE` |  | int |  |
| `--augment-anchors` | Apply memory_core._augment_embed_text_with_anchors to content before hashing. Required for non-chatlog types where memory_write_impl applied this transform at write time. Default OFF (chatlog uses raw content). | `False` |  | store_true |  |
| `--dry-run` | Count rows that would be updated; write nothing. | `False` |  | store_true |  |

---

## Environment variables read

- `M3_DATABASE`

---

## Calls INTO this repo (intra-repo imports)

- `memory_core`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `str(args.db)`` (line 177)
- `sqlite3.connect()  → `str(args.db)`` (line 180)
- `sqlite3.connect()  → `str(db_path)`` (line 148)
- `sqlite3.connect()  → `str(db_path)`` (line 70)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
