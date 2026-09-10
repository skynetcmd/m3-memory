# PostgreSQL → PostgreSQL sync (FDW fast-path)

*Advanced / optional. Most users don't need this — see [SYNC.md](SYNC.md) for the
default sync.*

If your **primary store is PostgreSQL** (`M3_DB_BACKEND=postgres`) **and** you sync
to a **PostgreSQL warehouse**, m3 can use a native PostgreSQL-to-PostgreSQL
fast-path instead of the default row-by-row bridge. It uses
[`postgres_fdw`](https://www.postgresql.org/docs/current/postgres-fdw.html) to run
each table's delta sync as a **single set-based `INSERT … SELECT … ON CONFLICT`**
on the server — bulk, streaming, no per-row Python round-trips.

**When it applies**

| Primary store | Warehouse | Sync path |
|---|---|---|
| SQLite (default) | PostgreSQL | Generic bridge (`pg_sync.py`) — unchanged, no setup |
| **PostgreSQL** | **PostgreSQL** | **FDW fast-path** (this page) |
| MariaDB (future) | PostgreSQL | Generic bridge (no FDW-to-Postgres) |

⚠ **On a PostgreSQL primary the fast path is not optional — it is the only path.**
If it can't run (extension missing, warehouse unreachable, permissions), m3
**refuses and exits non-zero** rather than falling back. The generic bridge opens
the local store as SQLite, so on a PG primary it would find no file — or worse, a
stale one — read zero rows, write zero rows, and report success. A sync that
silently moves nothing is worse than one that fails, so it fails.

The error names both the condition and the FDW reason behind it, e.g.:

```
Local store is 'postgres' (M3_DB_BACKEND), but the generic sync bridge
supports only a SQLite local store.
  FDW fast-path unavailable: postgres_fdw extension not installed on the primary
  Refusing to run rather than syncing an empty database.
  Fix: ensure M3_PRIMARY_PG_URL points at the primary store and, for the fast
  path, ask an administrator for `CREATE EXTENSION postgres_fdw;` on it.
```

A **SQLite** primary is unaffected: it uses the generic bridge as it always has,
and nothing on this page applies.

⚠ **`M3_PRIMARY_PG_URL` must point at your PRIMARY, not the warehouse.** Earlier
releases resolved the "primary" connection through the warehouse resolver, so the
fast path wired `postgres_fdw` from the warehouse back to itself and synced the
warehouse with itself — the primary was never touched, and the run reported
success. m3 now refuses when the two DSNs name the same `(host, port, dbname)`.

---

## What you need

1. **Both** ends are PostgreSQL: a PG primary (`M3_PRIMARY_PG_URL`) and a PG
   warehouse (`M3_CDW_PG_URL`).
2. The `postgres_fdw` **extension installed on the primary** (one-time, superuser).
3. The **sync role granted access to the warehouse schema** (`m3_warehouse`),
   one-time, on the warehouse, by a superuser.
4. Network reachability from the primary host to the warehouse host on the PG port.

---

## Setup

### 1. Install the FDW extension on the **primary** (superuser)

```sql
-- Connect to the PRIMARY database as a superuser (e.g. postgres):
CREATE EXTENSION IF NOT EXISTS postgres_fdw;
```

`postgres_fdw` ships with PostgreSQL but is not enabled by default. `CREATE
EXTENSION` requires superuser — a normal app role cannot enable it. Check it's
available first:

```sql
SELECT name, installed_version
FROM pg_available_extensions
WHERE name = 'postgres_fdw';
```

### 2. Grant the sync role access to the warehouse schema (superuser, on the **warehouse**)

m3 stores warehouse memory under the **`m3_warehouse`** schema (unified core +
chat-log). Your sync role (the user in `M3_CDW_PG_URL`) needs read/write there.
Run this on the **warehouse** database as a superuser (e.g. `postgres`):

```sql
GRANT USAGE ON SCHEMA m3_warehouse TO <sync_role>;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA m3_warehouse TO <sync_role>;
-- so tables created later are usable too:
ALTER DEFAULT PRIVILEGES IN SCHEMA m3_warehouse
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO <sync_role>;
```

Replace `<sync_role>` with the username in your `M3_CDW_PG_URL` (commonly
`agent_os`). **This grant is required for warehouse sync to work at all** — with
or without the FDW fast-path. Without it you'll see `permission denied for schema
m3_warehouse` (or, on the generic path, `relation "memory_items" does not exist`,
because the sync can't see the warehouse tables).

### 3. That's it — m3 wires the rest automatically

You do **not** create the foreign server or import tables by hand. When both ends
are PostgreSQL and the extension + grant are present, m3 sets up the
`postgres_fdw` foreign server, user mapping, and foreign-table import on each sync
run (idempotently), then runs the set-based upserts. Run sync as usual (see
[SYNC.md](SYNC.md#running-sync) — same command and schedule).

---

## How it works (and how conflicts resolve)

Each table syncs **both directions from the primary side** (which hosts the
foreign tables): *push* writes the warehouse, *pull* reads it. Each direction is
one statement:

```sql
INSERT INTO <target> (<cols>)
SELECT <cols> FROM <source>
WHERE updated_at > <last-watermark>          -- delta only
ON CONFLICT (id) DO UPDATE SET <cols=EXCLUDED.cols>
WHERE <target>.updated_at < EXCLUDED.updated_at;   -- last-writer-wins
```

- **Delta:** only rows changed since the last watermark are considered.
- **Last-writer-wins:** an incoming row only overwrites the target if it is
  *newer* (`updated_at`). An older row is silently skipped — a stale push can
  never clobber a fresher warehouse row, and vice-versa.
- **Tombstones:** soft-deletes (`is_deleted` / `updated_at`) propagate like any
  other change and stay recoverable.

Tables synced: `memory_items` (incl. chat-log rows), `memory_embeddings`,
`memory_relationships`, `tasks`, and `synchronized_secrets` — the **same five**
the generic bridge covers. Only the **columns shared** between your primary and
the warehouse are synced; local-only columns (belief/knowledge-graph fields) stay
on the primary by design.

> Earlier releases synced only the first three here, so a deployment on the fast
> path silently did not sync its tasks or its secrets vault — no error, those
> tables simply never moved. If you were on the fast path before this change,
> expect the first run afterwards to move a backlog of both.

**`synchronized_secrets` resolves conflicts by VERSION, not by timestamp.** The
version number is authoritative and `updated_at` only breaks ties within one
version, so a secret whose version was bumped without its timestamp moving still
propagates. Every other table is last-writer-wins on `updated_at`;
`memory_embeddings` and `memory_relationships` have no conflict guard at all — an
embedding is derived from its memory's content, so the newest is always right, and
a relationship edge is immutable.

---

## Gotchas

These are the real traps (some we hit building it):

- **The extension needs superuser; your app role can't self-enable it.** If you
  only have the app role, `CREATE EXTENSION` fails silently-ish — m3 detects the
  missing extension and falls back to the generic bridge rather than erroring. If
  you *expected* the fast-path and don't see it, check step 1 ran as superuser.
- **The `m3_warehouse` grant is the #1 cause of "sync does nothing".** The
  warehouse tables live in `m3_warehouse`, not `public`. A sync role that can
  reach `public` (so `tasks`/`secrets` sync fine) but lacks `m3_warehouse` USAGE
  will **silently fail memory/embedding sync** with `relation "memory_items" does
  not exist` — because `information_schema` hides tables the role can't see, so it
  looks like the table is absent. Grant the schema (step 2).
- **Password with special characters in the DSN:** put the password in the DSN
  URL-encoded (e.g. `@` → `%40`). m3 handles this, but if you build a DSN by hand
  in a shell, **don't** split it with `read`/`IFS` — shells mangle `|`, `@`, and
  trailing newlines and you'll get `password authentication failed` even though
  the password is correct. Let m3 (or a proper parser) read the DSN.
- **Trust/peer auth on the same host:** an FDW foreign server pointing at
  `localhost` with `trust` auth may fail to connect (the FDW connects as a TCP
  client and needs a real auth method). Real deployments have distinct
  primary/warehouse hosts with password auth, which is the supported case. A
  self-referential FDW (primary == warehouse host, trust auth) is not supported.
- **`wal_level` and logical replication:** the fast-path uses `postgres_fdw`, not
  logical replication, so you do **not** need `wal_level = logical` or a server
  restart. (Logical replication was considered and is heavier — a config change +
  restart on both servers — and isn't required here.)

---

## Caveats

- **PostgreSQL-only.** SQLite primaries (the default install) and MariaDB
  primaries use the generic row-by-row bridge — there is no FDW path to a
  PostgreSQL warehouse from those. This is by design; the fast-path is a pure
  PG→PG optimization.
- **Security — the foreign-server password lives in the PG catalog.** The FDW
  user mapping stores the warehouse password in the primary's catalog (visible to
  superusers of the primary). If that is unacceptable, use a `.pgpass` file or
  SCRAM channel binding on the primary instead of an inline password, and grant
  the sync role accordingly.
- **Bidirectional, single-driver.** Both directions run from the primary side.
  You don't need to configure a foreign server on the warehouse pointing back.
- **Automatic fallback is not a silent downgrade you should ignore.** If m3 falls
  back to the generic bridge, sync still works but slower. Check the sync log
  (`logs/sync_all.log`) for a `FdwUnavailable` / fallback line if you expected the
  fast-path and want to know why it didn't engage.
- **Vector search on a PG primary is currently brute-force cosine.** (Unrelated to
  sync, but relevant if you're standing up a PG primary — pgvector/HNSW ANN
  indexing is a future item; see [ARCHITECTURE.md](ARCHITECTURE.md).)

---

## Verifying it worked

After a sync, on the warehouse:

```sql
SELECT count(*), max(updated_at)
FROM m3_warehouse.memory_items;
-- count should climb toward your primary's memory_items count,
-- max(updated_at) should be recent.
```

If memory/embeddings counts stay flat while `tasks`/`secrets` sync fine, revisit
the `m3_warehouse` grant (step 2) — that's the classic symptom.
