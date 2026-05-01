---
tool: bin/m3_entities.py
sha1: 99984dd5e887
mtime_utc: 2026-05-01T09:23:06.299813+00:00
generated_utc: 2026-05-01T13:05:26.854551+00:00
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

- `def main()` (line 660)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--profile` | f'Profile name in config/slm/. Default: {DEFAULT_PROFILE}.' | `DEFAULT_PROFILE` |  | str |  |
| `--entity-vocab-yaml` | f'Vocab YAML path. Default: {DEFAULT_VOCAB_YAML}.' | None |  | str |  |
| `--core` | Only enrich the core memory DB (skip chatlog). | `False` |  | store_true |  |
| `--chatlog` | Only enrich the chatlog DB (skip core). | `False` |  | store_true |  |
| `--core-db` | Explicit path to the core memory DB. | None |  | str |  |
| `--chatlog-db` | Explicit path to the chatlog DB. | None |  | str |  |
| `--source-variant` | Filter source rows by variant. '__none__' = true core memory only (variant IS NULL). A name = single-variant scope. Default: no filter. | None |  | str |  |
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

---

## Calls INTO this repo (intra-repo imports)

- `agent_protocol (strip_code_fences)`
- `auth_utils (get_api_key)`
- `memory_core`
- `slm_intent (Profile, load_profile)`

---

## Calls OUT (external side-channels)

**http**

- `httpx.AsyncClient()` (line 374)
- `httpx.AsyncClient()` (line 421)

**sqlite**

- `sqlite3.connect()  → `f'file:{db_path}?mode=ro'`` (line 332)


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
