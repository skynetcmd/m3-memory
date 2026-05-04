---
tool: bin/migrate_entity_vocab.py
sha1: 9cca3baf43ca
mtime_utc: 2026-05-04T22:04:47.599369+00:00
generated_utc: 2026-05-04T22:24:29.488364+00:00
private: false
---

# bin/migrate_entity_vocab.py

## Purpose

One-shot migration: rename v1 entity vocabulary to v2-aligned names.

Performs in-place rename of entity_type and predicate values in any DB that
was extracted under the v1 vocabulary (default before 2026-05-03), and also
migrates 'contradicts' predicate rows that may have been extracted under
the m3 vocab (which dropped 'contradicts' alongside v2). Idempotent --
re-running has no effect on already-migrated rows since the old names no
longer exist after the first pass.

Renames performed (each affects ANY DB whose entity tables contain the
old names; the same script applies to both human-life DBs and technical
DBs since the renames are semantic substitutions):

  entities.entity_type:
    'concept'   -> 'legacy_concept'   (preserved-but-deprecated; v2-related)
    'object'    -> 'legacy_object'    (preserved-but-deprecated; v2-related)

  entity_relationships.predicate:
    'relates_to' -> 'mentions'    (v1 catch-all -> v2 catch-all)
    'contradicts' -> 'supersedes' (v1/m3 change-edge -> canonical change edge)

Left as-is (still valid in default schema as deprecated):
    'before', 'after'  predicate rows -- temporal ordering now derived from
        has_time edges, but old rows remain queryable.

Backward-compat: not maintained at the prompt level. v1 type names
('concept', 'object') and v1/m3 predicate names ('relates_to',
'contradicts') are NOT in the new default vocab; new extractors must use
the v2 / updated-m3 names. Existing rows under the old names would fail
validation on re-write until this script renames them. Reads remain fine
since validation only fires on the write path.

DBs that may need this migration: any SQLite DB with the entity_graph
tables (migration 024) that was extracted under the v1 default vocab or
the pre-2026-05-03 m3 vocab. Chatlog DBs without entity_relationships
tables are skipped at the schema-check.

Usage:
    python bin/migrate_entity_vocab.py --database <path/to/your.db>
    python bin/migrate_entity_vocab.py --database <path/to/your.db> --dry-run

The default DB resolution order (M3_DATABASE env > --database flag > default
agent_memory.db) follows the same convention as other m3-memory scripts.

---

## Entry points

- `def main()` (line 189)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--database` | Path to the SQLite DB to migrate. Default: M3_DATABASE env or memory/agent_memory.db. | `os.environ.get('M3_DATABASE') or str(REPO_ROOT / 'memory' / 'agent_memory.db')` |  | str |  |
| `--dry-run` | Audit row counts, print the plan, write nothing. | `False` |  | store_true |  |

---

## Environment variables read

- `M3_DATABASE`

---

## Calls INTO this repo (intra-repo imports)

- `m3_sdk (resolve_db_path)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 75)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
