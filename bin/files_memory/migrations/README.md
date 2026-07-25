# files_memory migrations

Schema for the **files store** (the file-ingestion subsystem), separate from the
primary memory store. Read this before adding a migration.

See also `memory/migrations/README.md` for the general SQLite-vs-PostgreSQL
migration conventions (type mapping, idempotent DDL, no `BEGIN`/`COMMIT` inside a
file) — those all apply here too. This README covers only what is *specific to the
files store*.

## Where the files store lives per backend

The files store is **physically separate** from the primary store, but that
separation is expressed differently per backend:

| Backend | Files store is… | Migrations dir | Table reference |
|---|---|---|---|
| SQLite | a separate **database file** (`files_database.db`) | `files_memory/migrations/` | bare `leaves` |
| PostgreSQL | a **schema namespace** (`files`) in the PRIMARY database (single DSN) | `files_memory/migrations/postgres/` | `files.leaves` |

On PG this is a **schema**, not a second database — a single DSN reaches it, so
cross-store promotion joins (`files.x JOIN public.memory_items`) work in-database.
Application code never uses `SET search_path`; it qualifies every files-store table
via the `files_table()` seam helper (`dialect().qualified_table(name,
schema="files")`) so a pooled PG connection carries no leaky session state. See
`~/.m3-private/plans/FILES_DB_TO_PG_PLAN.md` for the full rationale.

## Runner + version tracking

`files_memory.migrate.run_pending(conn)` applies pending up-migrations in numeric
order, tracking applied versions in a `schema_versions` table **inside the files
store** (the SQLite file, or the `files` schema on PG). `db.init_db()` calls it on
first touch. This is separate bookkeeping from the primary store's own
`schema_versions`.

## Adding a migration

Add a pair per backend, next free number **in each directory** (numbers are
independent, same as the primary store):

- SQLite: `NNN_<name>.up.sql` + `NNN_<name>.down.sql`
- PostgreSQL: `postgres/pg_NNN_<name>.up.sql` + `postgres/pg_NNN_<name>.down.sql`
  (create tables **in the `files` schema**: `CREATE TABLE files.<name>`).

Type mapping (SQLite → PG): `TEXT`→`TEXT`, `INTEGER`→`BIGINT`, `REAL`→`DOUBLE
PRECISION`, `BLOB`→`BYTEA`, `datetime('now')` default → `NOW()` / `TIMESTAMPTZ`.

## FTS5 is SQLite-only (Phase 1)

`leaves_fts` / `file_summaries_fts` (FTS5 external-content virtual tables + their
six sync triggers) exist **only in the SQLite migrations**. PostgreSQL has no FTS5;
the PG migrations deliberately OMIT them. Until the FTS → `tsvector`/GIN port
(Phase 2), `files_search` on PG runs the vector channel only. Do not add FTS5
constructs to a `pg_` migration.

## Provenance of 001/002/003

The SQLite `001`–`003` migrations were **generated** from the historical inline
blobs in `schema.py` (`SCHEMA_V1/2/3`) and proven byte-identical to what the old
`executescript` path produced. `schema.py` is now historical-source-only — do not
apply it at runtime. The `pg_001`–`pg_003` mirrors are the hand-authored PG port
(type-mapped, `files`-schema-qualified, FTS omitted).
