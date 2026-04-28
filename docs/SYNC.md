# Sync — keeping your memory in sync across machines

m3-memory's sync system keeps your memory database in sync between your local
machine and a central PostgreSQL warehouse. This lets you switch machines
(desktop ↔ laptop, work ↔ home) without losing context.

This page covers the **default sync** — `agent_memory.db` only. If you also
run benchmarks (most users don't), see [BENCH_SYNC.md](BENCH_SYNC.md).

## What gets synced

- **`memory/agent_memory.db`** — your production memory: notes, decisions,
  facts, conversations, embeddings, relationships. Bidirectional row-level
  delta sync to PostgreSQL via `bin/pg_sync.py`.
- **`memory/agent_chatlog.db`** — raw chat archive (Claude Code, Codex logs)
  awaiting promotion. Synced if the file exists; skipped silently if not.
- **ChromaDB embeddings** — vector store mirror, push/pull both directions
  via `bin/chroma_sync_cli.py`.

That's it. The repo does not ship sync support for bench result DBs or other
custom databases. If you self-host a more complex layout (e.g., separate
DBs for benchmarking), use `M3_SYNC_DBS` to add them — see the *Advanced*
section at the bottom — but the repo will not auto-detect them and there
is no shipped warehouse migration for non-default DBs.

## Setup

You need:

1. A reachable PostgreSQL server (your warehouse).
2. A reachable ChromaDB instance (typically on the same host as Postgres).
3. The connection string stored in your OS keyring or env var.

Set two env vars (typical values shown):

```bash
export POSTGRES_SERVER=10.21.40.51    # or SYNC_TARGET_IP — same thing
export PG_URL='postgresql://user:pass@host:5432/agent_memory'
```

Or store `PG_URL` in your OS keyring (macOS Keychain, Windows Credential
Manager, Linux Secret Service) — the codebase uses `auth_utils.get_api_key`
to look it up safely.

Apply the warehouse schema (one-time per warehouse). Postgres-side migrations
live in `memory/migrations/postgres/`:

```bash
psql -h $POSTGRES_SERVER -U $PGUSER -d agent_memory \
  -f memory/migrations/postgres/pg_warehouse_chatlog_v1.sql
```

Note: `memory/migrations/*.sql` (without the `postgres/` subdir) are SQLite
migrations applied automatically on first connect. Don't put Postgres SQL
there — `migrate_memory` will warn about malformed files.

## Running sync

Manually:

```bash
python bin/sync_all.py
```

What it does:

1. TCP-probes `$POSTGRES_SERVER` (3-second timeout).
2. If reachable, runs `bin/pg_sync.py` for `agent_memory.db`.
3. Runs `bin/chroma_sync_cli.py both` (bidirectional).
4. Logs to `logs/sync_all.log`.

Dry-run (just check connectivity, don't write):

```bash
python bin/sync_all.py --dry-run
```

## Scheduling

For unattended hourly sync:

- **Linux/macOS**: cron — see `bin/pg_sync.sh` for a wrapper that handles env.
- **Windows**: Scheduled Task. Action: `python.exe bin/sync_all.py`.
  Working dir: the repo root. Run as your user, schedule hourly.

The scheduler tolerates outages — if the warehouse is unreachable, sync logs
a warning and exits cleanly. Next run picks up where it left off.

## How conflict resolution works

`pg_sync.py` uses **last-write-wins** based on `updated_at`. When the same
row exists in both SQLite and Postgres with different `updated_at` values,
the newer one wins. This means:

- Edit a note on machine A, sync → warehouse has A's version.
- Edit the same note on machine B before A's sync reaches B, then sync →
  whichever has the later `updated_at` wins.
- Soft-deletes (rows with `is_deleted=1`) propagate cleanly. The deleted
  state replicates; the row stays in both DBs marked deleted.

## What does NOT sync by default

- **Bench result DBs** — out of scope for the repo. If you run benchmarks
  and want their results synced across machines, that's self-host territory:
  add the DBs to `M3_SYNC_DBS` and provide your own warehouse schema migration.
- **`memory/local_*` rows** — anything tagged `scope='local'` is per-machine
  by design.
- **`/tmp` scratch and `.scratch/`** — these are workspace, not memory.

## Troubleshooting

**"PostgreSQL data warehouse unreachable"** → TCP probe failed. Check:
- Is `POSTGRES_SERVER` set?
- Can you `nc -zv $POSTGRES_SERVER 5432` from this host?
- Is your warehouse running?

**"Another sync is already in progress"** → A previous sync hung. Look in
`logs/sync_all.log` for orphaned PIDs. The lock file is at
`memory/.pg_sync.lock`; remove it manually if stale.

**"chroma_sync timed out after 120s"** → ChromaDB is slow or wedged. Check:
- ChromaDB heartbeat: `curl -m 5 http://$POSTGRES_SERVER:8000/api/v2/heartbeat`
- ChromaDB queue size — if growing without bound, the service may need a
  restart. Out of scope here; see your warehouse host's docs.

**"Schema mismatch / missing column"** → You haven't applied the latest
warehouse migration. See setup, step 3.

**Hourly task stops running on Windows** → Task may auto-disable after
repeated failures. Check `schtasks /Query /FO LIST /V | grep -i m3-memory`
for `Status: Disabled`. Re-enable with `schtasks /Change /TN "<name>" /ENABLE`.

## Multi-machine quick reference

Setting up a second machine to sync against the same warehouse:

1. Clone the repo on machine B.
2. Set `POSTGRES_SERVER` and `PG_URL` env vars (same warehouse as A).
3. First sync pulls everything from the warehouse — let it finish.
4. From then on, edits on either machine appear on the other after sync.

Three-way sync (A ↔ warehouse ↔ B) works the same — the warehouse is the
hub; peers don't talk to each other directly.

---

## Advanced: M3_SYNC_DBS

If you want to override the default DB list (e.g., to sync a custom
named DB, or to skip auto-detection of bench DBs even when present):

```bash
# Sync only agent_memory.db (no auto-detect)
M3_SYNC_DBS=memory/agent_memory.db python bin/sync_all.py

# Sync a custom set
M3_SYNC_DBS=memory/agent_memory.db:custom/extra.db python bin/sync_all.py
```

Paths can be colon- or comma-separated, absolute or relative to the repo root.
