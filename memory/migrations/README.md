# Schema migrations

Read this before adding a migration. The two backends (SQLite and PostgreSQL)
have **independently-numbered** migration sequences, and that trips people up.

## TL;DR — adding a schema change

A schema change usually needs **two** migration files, one per backend, each the
next free number **in its own sequence**:

| Backend | Directory | File pattern | Applied by |
|---|---|---|---|
| SQLite (primary default) | `memory/migrations/` | `NNN_<name>.up.sql` + `NNN_<name>.down.sql` | `bin/migrate_memory.py` |
| PostgreSQL (optional primary) | `memory/migrations/postgres/` | `pg_MMM_<name>.up.sql` + `pg_MMM_<name>.down.sql` | `bin/migrate_pg.py` |

Both runners discover files by `sorted(glob(...))` and take the **numeric prefix**
as the version. So the next SQLite file is `MAX(NNN)+1` in `memory/migrations/`,
and the next PG file is `MAX(MMM)+1` in `memory/migrations/postgres/` — **computed
separately.** They are *not* required to share a number.

`tests/test_schema_parity_pg_live.py` diffs the two **live schemas**, not the file
numbers — that is what guarantees the two backends end up describing the same
tables. Keep the tables identical; the numbers may differ.

## Why the numbers diverge (the confusing part)

**The two sequences do not share history.** SQLite evolved organically, one file
per change, from `001`. When the PostgreSQL primary backend was added later, nobody
replayed 40 tiny SQLite migrations against PG. Instead the entire schema *as of
~SQLite 039* was hand-written into a single cumulative baseline,
`postgres/pg_primary_v1.sql`, stamped **version 39** (`bin/migrate_pg.py`,
`_BASELINE_VERSION = 39`). PG's own incremental files then continue from `pg_040`.

So PG's counter runs **ahead** of SQLite's for a mundane reason: several of PG's
`pg_04x` files are **parity catch-up** — they add tables/columns SQLite already had
"for free" inside earlier migrations that the hand-written baseline missed or
drifted on (caught by the parity test). Example: `pg_044_parity_gdpr_and_stage`
adds `gdpr_requests` (SQLite got it in `010_tier_features.sql`) and a couple of
`stage` columns (SQLite `026`). PG needed *extra numbered files* to reach a schema
SQLite already had — that debt is why PG's top number sits above SQLite's.

**These counters keep drifting** — every schema change bumps one or both, and a PG
parity-catch-up bumps only PG, so the gap between them is not fixed. **Do not read
any specific pair of top numbers here as canonical.** The rule is always the same:
```
next SQLite version = 1 + max numeric prefix in  memory/migrations/*.sql
next PG version      = 1 + max numeric prefix in  memory/migrations/postgres/pg_*.sql
```
Compute each from the directory listing at the time you add a file — never assume
"they should both be N", and never try to "sync" the two counters to match.

**This offset is expected, not a bug.**

## Rules

- **Type mapping (SQLite → PG):** `TEXT`→`TEXT`, `INTEGER`→`BIGINT`,
  `REAL`→`DOUBLE PRECISION`, `strftime(...)` default →`NOW()` / `TIMESTAMPTZ`.
  Mirror the *intent*, not the literal DDL.
- **Idempotent DDL both sides:** `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF
  NOT EXISTS`, `ADD COLUMN IF NOT EXISTS` (PG). Both runners wrap a file in one
  transaction, so **no** `BEGIN`/`COMMIT`/`ROLLBACK` inside a migration file.
- **SQLite file forms** (both supported, sorted by numeric prefix): `NNN_name.sql`
  (legacy, up-only), or the explicit `NNN_name.up.sql` + `NNN_name.down.sql` pair.
  Prefer the explicit pair.
- **`schema_versions`** records applied migrations; each up/down takes a
  filesystem-level DB backup first, so operations are reversible at the file level.
- **After adding a pair, sanity-check parity locally.** The SQLite up-migration
  should apply to a scratch `:memory:` DB, and the PG variant should produce the
  same logical tables/indexes. `test_schema_parity_pg_live.py` is the CI gate.
- **The parity test only guards tables in its explicit `_SHARED_CORE_TABLES`
  set.** It diffs the two live schemas *for the tables it is told to compare* — a
  table missing from BOTH the PG schema and that set ships undetected (this is
  exactly how `agent_retention_policies` — SQLite `010`, never mirrored to PG —
  went unnoticed until it surfaced as a live PG runtime error, fixed by
  `pg_046_parity_agent_retention`). **When you add a shared table, add it to
  `_SHARED_CORE_TABLES` in the same change** so the gate can catch future drift.

## Numbering at a glance (why they're offset)

Shapes, not fixed numbers — the exact top values move over time:

```
SQLite  memory/migrations/           PostgreSQL  memory/migrations/postgres/
  001 ... (organic, one file/change)   pg_primary_v1.sql  == cumulative baseline
                                                              (stamped version 39)
  ... continues incrementally          pg_040 ...          (incrementals from 40)
                                       + parity-catchup files that re-add schema
                                         SQLite already had inside its early
                                         history — these bump ONLY PG's counter,
                                         so PG stays ahead by a drifting margin.
```

Authoritative source for the current top of each sequence is always the directory
listing itself, not this diagram.
