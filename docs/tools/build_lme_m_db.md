---
tool: bin/build_lme_m_db.py
sha1: 303d70294d8d
mtime_utc: 2026-05-03T21:28:26.613102+00:00
generated_utc: 2026-05-04T22:24:29.012006+00:00
private: false
---

# bin/build_lme_m_db.py

## Purpose

build_lme_m_db.py — build memory/lme_m.db from longmemeval_m_cleaned.json.

Reference DB for post-bench analysis. Three tables:

    lme_m_questions     — 500 rows, one per question. Question-level
                          metadata + gold_verified flag for manual audit.
    lme_m_conversations — ~241K rows, one per (question_id, haystack_idx).
                          Session-level metadata + concatenated raw text +
                          size + is_gold flag + chunk-count estimates.
    lme_m_turns         — ~2.45M rows, one per turn. Lets analysis SQL
                          ask "which user turn within the gold session
                          contains the evidence?" without reparsing JSON.

Usage:

    # Inspect what would happen, no write
    python bin/build_lme_m_db.py --dry-run

    # Build (idempotent — re-running with same source-of-truth is a no-op)
    python bin/build_lme_m_db.py

    # Force-rebuild (drops + recreates tables)
    python bin/build_lme_m_db.py --rebuild

---

## Entry points

- `def main()` (line 135)
- `if __name__ == "__main__"` guard

---

## CLI flags / arguments

| Flag(s) | Help | Default | Default behavior | Type/Action | Impact when set |
|---|---|---|---|---|---|
| `--source` | f'Path to the LME-M JSON. Default: {DEFAULT_SOURCE}' | `DEFAULT_SOURCE` |  | Path |  |
| `--db` | f'Output SQLite path. Default: {DEFAULT_DB}' | `DEFAULT_DB` |  | Path |  |
| `--dry-run` | Print what would be written; don't touch the DB. | `False` |  | store_true |  |
| `--rebuild` | Drop the three tables before rebuilding. Without this flag the script aborts if any of the tables already contain rows (idempotent-by-default). | `False` |  | store_true |  |

---

## Environment variables read

_(none detected)_

---

## Calls INTO this repo (intra-repo imports)

_(none detected)_

---

## Calls OUT (external side-channels)

**sqlite**

- `sqlite3.connect()  → `str(args.db)`` (line 207)


---

## Notable external imports

_(only stdlib)_

---

## File dependencies (repo paths referenced)

- `lme_m.db`
- `longmemeval_m_cleaned.json`

---

## Re-validation

If the `sha1` above differs from the current file's sha1, the inventory is stale — re-read the tool, confirm flags/env vars/entry-points/calls still match, and regenerate via `python bin/gen_tool_inventory.py`.
