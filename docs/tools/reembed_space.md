---
tool: bin/reembed_space.py
sha1: 20cc84f875d6
mtime_utc: 2026-08-30T00:09:08.520688+00:00
generated_utc: 2026-08-30T01:24:29.409422+00:00
private: false
---

# bin/reembed_space.py

## Purpose

reembed_space.py — retire vectors from the wrong embedding model.

Companion to the mixed-embed-space doctor probe (bin/doctor/embed_space_probe.py).
When a store has accumulated vectors from more than one embedding model, cosine
across those spaces is meaningless and the minority rows rank wrongly forever —
silently, with no error. This tool retires the offending vectors so they can be
regenerated with the current model.

DESIGN: this does NOT contain an embed loop. It deletes the stale
``memory_embeddings`` rows, which makes their parent items match the
``WHERE NOT EXISTS`` predicate that ``bin/embed_backfill.py`` already sweeps on.
That sweeper is hardened (batching, concurrency, timeouts, oversize/bad-dim
skips, resumability) and re-implementing it here would be a second code path to
keep correct. So the flow is:

    m3 embedder reembed --apply      # retire stale vectors (this tool)
    python bin/embed_backfill.py     # regenerate them (existing sweeper)

``--apply`` chains the sweeper automatically unless ``--no-backfill`` is given.

SAFETY: dry-run is the DEFAULT. The tool prints exactly what it would delete and
exits without touching anything until ``--apply`` is passed. A timestamped backup
of the target DB is taken before the first delete unless ``--no-backup`` is set.
Deleting an embedding is non-destructive to the MEMORY — content, metadata and
relationships are untouched; only the vector is dropped and regenerated.

---

## Entry points

- `def main()` (line 243)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--db` | Target DB (default: the resolved engine agent_memory.db). | None |  | str |  |
| `--keep` | Model family to KEEP (e.g. 'bge-m3'). Default: the family holding the most vectors. | None |  | str |  |
| `--apply` | Actually delete. Without this the tool only reports. | `False` |  | store_true |  |
| `--no-backup` | Skip the pre-delete DB copy (not recommended). | `False` |  | store_true |  |
| `--no-backfill` | Do not chain embed_backfill.py after deleting. | `False` |  | store_true |  |
| `--all-dbs` | Process BOTH engine stores (agent_memory.db and agent_chatlog.db). File backend only. Ignores --db. | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `_task_runtime (no_window_kwargs)`
- `sqlite_pragmas (apply_pragmas, profile_for_db)`
- `sqlite_pragmas (checkpoint_truncate)`

---

## Calls OUT (external side-channels)

**subprocess**

- `subprocess.call()  → `cmd`` (line 400)

**sqlite**

- `sqlite3.connect()  → `db_path`` (line 191)


---

## Notable external imports

- `doctor.embed_space_probe (_family)`
- `m3_core.paths (resolve_engine_file)`
- `memory.backends (active_backend)`

---

## File dependencies (repo paths referenced)

- `agent_chatlog.db`
- `agent_memory.db`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
