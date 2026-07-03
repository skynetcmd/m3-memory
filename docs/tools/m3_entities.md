---
tool: bin/m3_entities.py
sha1: 2c8df28439ae
mtime_utc: 2026-07-02T21:51:11.647462+00:00
generated_utc: 2026-07-03T20:00:03.590168+00:00
private: false
---

# bin/m3_entities.py

## Purpose

m3_entities — build entity-graph rows from your core/chatlog DBs.

Phase I driver. Mirrors `bin/m3_enrich.py`'s shape: profile-based,
generic source-variant filter, --core / --chatlog scope flags, --dry-run
preview, smoke-then-full-pass workflow.

What it does:
  - Walks eligible memory_items rows (filtered by type-allowlist +
    --source-variant).
  - For each row, calls the extractor (default: qwen/qwen3-8b:2 via
    LM Studio Anthropic /v1/messages) with the m3-tuned vocab and
    tightened prompt.
  - Resolves entities via memory_core helpers (idempotent UPSERT into
    `entities` table; INSERT OR IGNORE on `memory_item_entities`).
  - Writes relationships into `entity_relationships` (delete-then-insert
    keyed on source_memory_id; idempotent re-extraction).
  - Records partial-failure metrics so re-running picks up where the
    last call left off.

What it does NOT do:
  - It is not a daemon; one-shot pass over the eligible set.
  - It does not re-extract rows that already have entities linked
    UNLESS --force is passed.

Usage examples:
  # Preview
  python bin/m3_entities.py --core --source-variant __none__ --dry-run

  # Smoke 10 rows
  python bin/m3_entities.py --core --source-variant __none__       --limit 10 --skip-preflight --yes

  # Full core pass
  python bin/m3_entities.py --core --source-variant __none__       --concurrency 4 --skip-preflight --yes

The default vocab is `config/lists/entity_graph_m3.yaml` (m3-tuned).
Override via --entity-vocab-yaml or M3_ENTITY_VOCAB_YAML.

---

## Entry points

- `def main()` (line 896)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--profile` | f'Profile name in config/slm/. Default: {DEFAULT_PROFILE}.' | `DEFAULT_PROFILE` |  | str |  |
| `--entity-vocab-yaml` | f'Vocab YAML path. Default: {DEFAULT_VOCAB_YAML}.' | None |  | str |  |
| `--embed-url` | Hard override for the embedder endpoint URL (e.g. http://127.0.0.1:8081/v1). Bypasses get_best_embed discovery — use when you need deterministic routing under concurrent load. Env: M3_EMBED_URL. | `os.environ.get('M3_EMBED_URL')` |  | str |  |
| `--embed-model` | Model id to send to the override endpoint. llama.cpp default: 'bge-m3-GGUF-Q4_K_M.gguf'. LM Studio: 'text-embedding-bge-m3'. Required only when --embed-url is set and the default model id is wrong for that server. Env: M3_EMBED_MODEL. | `os.environ.get('M3_EMBED_MODEL')` |  | str |  |
| `--core` | Only enrich the core memory DB (skip chatlog). | `False` |  | store_true |  |
| `--chatlog` | Only enrich the chatlog DB (skip core). | `False` |  | store_true |  |
| `--core-db` | Explicit path to the core memory DB. | None |  | str |  |
| `--chatlog-db` | Explicit path to the chatlog DB. | None |  | str |  |
| `--source-variant` | Filter source rows by variant. '__none__' = true core memory only (variant IS NULL). A name = single-variant scope. Default: no filter. | None |  | str |  |
| `--source-conv-list` | Path to a file listing conversation_ids to scope extraction to. Format: newline-delimited text (with optional # comments) OR a JSON array. Filtering reads metadata_json.$.conversation_id, falling back to the memory_items.conversation_id column. Narrows the eligible set AFTER --source-variant + type filtering. Env: M3_ENTITIES_CONV_LIST. | `os.environ.get('M3_ENTITIES_CONV_LIST')` |  | str |  |
| `--types` | Comma-separated type allowlist override. Default: chat + curated. | None |  | str |  |
| `--limit` | Cap rows enriched per DB (smoke testing). | None |  | int |  |
| `--concurrency` | Concurrent SLM calls. Default 2 (single-host LM Studio with two qwen3-8b instances was OOM-reloading at 4). | `2` |  | int |  |
| `--force` | Re-extract rows that already have memory_item_entities. Default: skip already-extracted. | `False` |  | store_true |  |
| `--dry-run` | Preview what would happen without writing. | `False` |  | store_true |  |
| `--skip-preflight` | Skip endpoint smoke + DB backup. Power-user only. | `False` |  | store_true |  |
| `--yes`, `-y` | Skip the interactive confirm prompt. | `False` |  | store_true |  |

---

## Environment variables read

- `M3_DATABASE`
- `M3_EMBED_MODEL`
- `M3_EMBED_URL`
- `M3_ENTITIES_CONV_LIST`
- `M3_ENTITY_VOCAB_YAML`

---

## Calls INTO this repo (intra-repo imports)

- `agent_protocol (strip_code_fences)`
- `auth_utils (get_api_key)`
- `m3_sdk (get_m3_root)`
- `memory_core`
- `slm_intent (Profile, load_profile)`

---

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 467)
- `httpx.AsyncClient()` (line 590)

**sqlite**

- `sqlite3.connect()  → `f'file:{db_path}?mode=ro'`` (line 398)


---

## Notable external imports

- `httpx`

---

## File dependencies (repo paths referenced)

- `.json`
- `.md`
- `.sql`
- `.txt`
- `.yaml`
- `.yml`
- `agent_chatlog.db`
- `agent_memory.db`
- `entity_graph_m3.yaml`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
