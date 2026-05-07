---
tool: bin/m3_enrich_assign.py
sha1: 9d017a26b0eb
mtime_utc: 2026-05-06T05:08:27.887590+00:00
generated_utc: 2026-05-06T23:11:45.213987+00:00
private: false
---

# bin/m3_enrich_assign.py

## Purpose

m3_enrich_assign.py — assign enrichment_groups.send_to for routed runs.

Updates the `send_to` column on rows matching a predicate, so that a
later `m3_enrich.py --send-to <name>` run claims only its assigned
rows. Use when running multiple providers (e.g. Grok + Gemini) against
the same source variant in parallel and you want disjoint pools by
explicit assignment rather than by accidental bucket isolation.

Common patterns:

    # Route all groups <= 7 KB to "grok"
    m3_enrich_assign --db memory/agent_test_bench.db \
                     --source-variant LME-M-ingestion \
                     --target-variant m3-observations-bench-LME-M-ingestion-20260428 \
                     --max-size-k 7 --send-to grok

    # Route the rest (>= 16 KB) to "gemini", leaving the 8-15 KB middle
    # band unassigned (NULL) for later
    m3_enrich_assign --db memory/agent_test_bench.db \
                     --source-variant LME-M-ingestion \
                     --target-variant m3-observations-bench-LME-M-ingestion-20260428 \
                     --min-size-k 16 --send-to gemini

    # Dry-run first
    m3_enrich_assign ... --send-to grok --dry-run

The script only touches `pending` and `failed` rows by default — it
won't reassign rows that already succeeded. Use --include-completed to
override.

---

## Entry points

- `def main()` (line 43)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--db` | Path to the database with enrichment_groups. | — |  | Path |  |
| `--source-variant` | Match enrichment_groups.source_variant. | — |  | str |  |
| `--target-variant` | Match enrichment_groups.target_variant. | — |  | str |  |
| `--send-to` | Provider name to assign (e.g. 'grok', 'gemini'). Pass the literal string 'NULL' to clear assignments. | — |  | str |  |
| `--min-size-k` | Only assign rows whose content_size_k >= N. | — |  | int |  |
| `--max-size-k` | Only assign rows whose content_size_k <= N. | — |  | int |  |
| `--only-unassigned` | Only assign rows where send_to IS NULL. Use to avoid clobbering an existing routing scheme. | `False` |  | store_true |  |
| `--include-completed` | Also reassign rows in success/empty/dead_letter status. Default: only pending and failed. | `False` |  | store_true |  |
| `--dry-run` | Print what would be updated; don't write. | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

- `sqlite_pragmas (apply_pragmas, profile_for_db)`

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `str(args.db)`` (line 72)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

_(none detected)_

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
