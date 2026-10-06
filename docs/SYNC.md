# Sync — keeping your memory in sync across machines

m3-memory's sync system keeps your memory database in sync between your local
machine and a central PostgreSQL warehouse. This lets you switch machines
(desktop ↔ laptop, work ↔ home) without losing context.

This page covers the **default sync** — a SQLite local store (`agent_memory.db`)
to a PostgreSQL warehouse. Bench result databases are **not** synced and are not
auto-detected; if you run benchmarks (most users don't) and want their DBs
included, add them to `M3_SYNC_DBS` and supply your own warehouse schema — see
[What does NOT sync by default](#what-does-not-sync-by-default) below.

> **If your local store is PostgreSQL**, this page's row-by-row bridge does not
> apply to you — it opens the local side as SQLite. Use
> [SYNC_PG_TO_PG.md](SYNC_PG_TO_PG.md) instead, which is the only supported path
> for a PG primary. m3 refuses rather than falling back, because falling back
> would sync nothing while reporting success.

## What gets synced

Both stores live in the engine root (`~/.m3/engine` by default; `M3_ENGINE_ROOT`
overrides):

- **`agent_memory.db`** — your production memory: notes, decisions,
  facts, conversations, embeddings, relationships. Bidirectional row-level
  delta sync to PostgreSQL via `bin/pg_sync.py`.
- **`agent_chatlog.db`** — raw chat archive (Claude Code, Codex logs)
  awaiting promotion. Synced if the file exists. If the chat log resolves to
  the main store (a unified install), the two sync as one and the log says so.

That's it. The repo does not ship sync support for bench result DBs or other
custom databases. If you self-host a more complex layout (e.g., separate
DBs for benchmarking), use `M3_SYNC_DBS` to add them — see the *Advanced*
section at the bottom — but the repo will not auto-detect them and there
is no shipped warehouse migration for non-default DBs.

---

## Setup

You need:

1. A reachable PostgreSQL server (your warehouse).
2. Its connection string (DSN), where the scheduled sync can read it.

**Store the DSN in m3's encrypted vault** under the name `PG_URL`. This is the
recommended place: the scheduled sync runs under launchd, systemd, cron or Task
Scheduler, none of which sees your shell's environment, but all of which can
read the vault. Run the payload's interactive helper and enter `PG_URL` as the
service name:

```bash
python <payload>/bin/setup_secret.py     # the payload path: `m3 doctor` prints it ("resolved bridge")
```

For a manual run you can use an environment variable instead; it takes
precedence over the vault:

```bash
export M3_CDW_PG_URL='postgresql://user:pass@db.example.com:5432/agent_memory'   # PG_URL works too, deprecated
```

Sync takes the warehouse host from the DSN. `M3_POSTGRES_SERVER` (or
`SYNC_TARGET_IP`) is optional and only overrides the host used for the
reachability check.

Apply the warehouse schema (one-time per warehouse). Postgres-side migrations
live in `memory/migrations/postgres/`:

```bash
psql -h db.example.com -U $PGUSER -d agent_memory \
  -f memory/migrations/postgres/pg_warehouse_chatlog_v1.sql
```

Note: `memory/migrations/*.sql` (without the `postgres/` subdir) are SQLite
migrations applied automatically on first connect. Don't put Postgres SQL
there — `migrate_memory` will warn about malformed files.

---

## Running sync

Manually:

```bash
python bin/sync_all.py
```

What it does:

1. TCP-probes the warehouse host (3-second timeout); the log names the host and
   where it came from.
2. If reachable, runs `bin/pg_sync.py` for each store
   (`Starting synchronization for 2 targets: ['main', 'chatlog']`).
3. Logs to `~/.m3/logs/sync_all.log` (`M3_LOGS_ROOT` overrides).

Dry-run (just check connectivity, don't write):

```bash
python bin/sync_all.py --dry-run
```

---

## Scheduling

`m3 setup` installs the hourly sync for you, and keeps it installed even though
the cognitive loop also syncs (the loop can be paused under heavy load; the
hourly job is the floor):

| OS | Installed as |
|---|---|
| macOS | launchd agent `~/Library/LaunchAgents/com.m3memory.sync_all.plist` (`StartInterval` 3600) |
| Linux | a line in m3's managed crontab block |
| Windows | Task Scheduler task `AgentOS_HourlySync` |

Check it with `m3 schedules verify` (each job's interval and command against
the spec), and restore it with `m3 schedules repair`. On macOS, repair replaces
a hand-made `com.m3memory.sync_all.plist` and saves the old one as a `.bak-` copy.

The scheduler tolerates outages — if the warehouse is unreachable, sync logs
a warning and exits cleanly. Next run picks up where it left off.

---

## How conflict resolution works

`pg_sync.py` uses **last-write-wins** based on `updated_at`. When the same
row exists in both SQLite and Postgres with different `updated_at` values,
the newer one wins. This means:

- Edit a note on machine A, sync → warehouse has A's version.
- Edit the same note on machine B before A's sync reaches B, then sync →
  whichever has the later `updated_at` wins.
- Soft-deletes (rows with `is_deleted=1`) propagate cleanly. The deleted
  state replicates; the row stays in both DBs marked deleted.

> **PostgreSQL primary?** If your primary store is PostgreSQL (not the default
> SQLite) *and* you sync to a PostgreSQL warehouse, m3 can use a faster native
> PostgreSQL-to-PostgreSQL path (`postgres_fdw`, set-based upserts) instead of
> this row-by-row bridge. Same conflict rules; extra one-time setup. See
> [SYNC_PG_TO_PG.md](SYNC_PG_TO_PG.md).

---

## What does NOT sync by default

- **Bench result DBs** — out of scope for the repo. If you run benchmarks
  and want their results synced across machines, that's self-host territory:
  add the DBs to `M3_SYNC_DBS` and provide your own warehouse schema migration.
- **`memory/local_*` rows** — anything tagged `scope='local'` is per-machine
  by design.
- **`/tmp` scratch and `.scratch/`** — these are workspace, not memory.

---

## Troubleshooting

**"No warehouse configured … skipping sync"** → the job found no DSN. Store
`PG_URL` in the vault (see Setup); an `export` in your shell rc is invisible to
scheduled jobs.

**"PostgreSQL data warehouse (host:port, from …) unreachable"** → TCP probe
failed. Check:
- Can you `nc -zv <host> <port>` from this host?
- Is your warehouse running, and is the network path (VPN/tailnet) up?

**Only `['main']` is synced, no chatlog** → the chat log resolved to the main
store, usually because the job sets `M3_DATABASE`. Run `m3 setup` once (it pins
the chat log's own path), or set `M3_CHATLOG_DB_PATH`.

**"pg_sync SKIPPED … (EX_TEMPFAIL)"** → the run could not take the sync lock
and replicated nothing; the next scheduled run retries. The lock is a row in the
`sync_locks` table (not a file) and a stale one is reclaimed automatically. The
log's `observed` line says whether another sync held it or the store was busy;
run `python bin/pg_sync.py --db <path>` by hand for the full reason.

**"Schema mismatch / missing column"** → You haven't applied the latest
warehouse migration. See setup.

**Hourly task stops running on Windows** → Task may auto-disable after
repeated failures. Check `schtasks /Query /TN AgentOS_HourlySync /V /FO LIST`
for `Status: Disabled`. Re-enable with `schtasks /Change /TN AgentOS_HourlySync /ENABLE`,
or run `m3 schedules repair`.

---

## Multi-machine quick reference

Setting up a second machine to sync against the same warehouse:

1. Install m3 on machine B (`pipx install m3-memory && m3 setup`).
2. Store the same warehouse DSN in B's vault as `PG_URL` (see Setup). Vault
   rows are encrypted with a key derived from `AGENT_OS_MASTER_KEY` plus a
   per-device salt, and sync replicates them by name — so B can decrypt a
   replicated `PG_URL` only if it shares A's master key and salt
   (`.agent_os_salt` in the config root, or `M3_AGENT_OS_SALT_HEX`). If it
   doesn't, give B's scheduled job the DSN via `M3_CDW_PG_URL` in the
   environment that job runs under instead.
3. First sync pulls everything from the warehouse — let it finish.
4. From then on, edits on either machine appear on the other after sync.

Three-way sync (A ↔ warehouse ↔ B) works the same — the warehouse is the
hub; peers don't talk to each other directly.

---

## Agent machine with no local m3 (MCP-only) — and why the chat log goes missing

A common setup: **machine A** runs m3, **machine B** runs the coding agent
(Claude Code, OpenCode) and reaches A's MCP server over the network. The memory
tools work, but nothing is captured to the chat log.

That is expected. **Chat capture is not part of the MCP server** — it is a hook
that the agent fires in its own process:

```
agent on machine B
   ├─▶ hooks/chatlog/opencode_session_end.py
   │        └─▶ chatlog_ingest.py        ← runs on machine B
   └─▶ MCP ─────────────────────────────▶ memory_search / memory_write on machine A
```

MCP carries the *tools* across the wire; it does not carry turn capture. m3 ships
hooks for Claude Code, OpenCode, Gemini CLI and Aider, and each one runs locally.

**So m3 has to be installed on machine B too.** Install it thin, then sync via
the warehouse as described above.

### Don't load a second embedder on the agent machine

The usual reason a small agent box falls over after installing m3 is the local
GGUF embedder: `m3-embed-server` loads `bge-m3-Q4_K_M.gguf` into RAM at startup.
On a 4-core / 16 GB machine, alongside the agent, that is enough to exhaust
memory.

Point machine B at machine A's embed server, and keep B from starting one of its
own:

```bash
# machine B
M3_EMBED_URL=http://machine-a:8082   # where to send embed requests
M3_EMBED_INPROC=0                    # do not load a local GGUF in-process
```

`M3_EMBED_URL` alone is not sufficient. Tier-1 **auto-detects** a bge-m3 GGUF in
the canonical model directories when `M3_EMBED_GGUF` is unset
(`M3_EMBED_GGUF_AUTODETECT` defaults to `1`), so a machine that happens to have
one — via LM Studio, say — will still load it in-process. In-process embedding is
gated by `M3_EMBED_INPROC` / `.embed_config.json`: the design is
*safe-by-default — route to the shared server unless inproc is clearly intended*,
so being explicit here keeps B on that path.

Then don't register the heavyweight services on B — skip `AgentOS_EmbedServer`
(it is the sole embedder for the fleet, and belongs on A), and skip the cognitive
loop if B doesn't need it. B needs the chatlog hooks, the MCP client, and its
local SQLite store; the warehouse sync handles the rest.

> ⚠️ The embed server binds `127.0.0.1` by default and has **no authentication**.
> To serve another machine, A must bind a reachable interface — keep that on a
> trusted LAN or a tunnel (Tailscale, WireGuard), never the open internet.

### Never share a SQLite file over a network mount

Do not put `agent_memory.db` on an SMB/NFS share and point both machines at it.
SQLite's locking relies on filesystem semantics network mounts don't reliably
provide. It appears to work, which is what makes it dangerous — the failure
surfaces later as a corrupt store or silently lost turns. Each machine keeps its
own local SQLite; the warehouse is the meeting point.

### Pin both roots, or the chat log splits

The **MCP server** reads its roots from its own registration's `env` block; the
**chatlog hook** inherits the agent's *process* environment. Pin only one and the
server reads the new root while hooks keep writing to the old one.

On machine B, set `M3_ENGINE_ROOT` and `M3_CONFIG_ROOT` in **both** the
`claude mcp add --env …` registration **and** inline on each hook `command`.

### Verifying capture on the agent machine

```bash
m3 chatlog doctor          # exits nonzero on capture warnings
m3 chatlog status --json   # last_write_at must advance after a session
```

If `last_write_at` doesn't move after a session on B, the hooks aren't firing —
chase that independently of whether the MCP tools work.

---

## Advanced: M3_SYNC_DBS

If you want to override the default DB list (e.g., to sync a custom
named DB). The override **replaces** the default list rather than adding to it:

```bash
# Sync only agent_memory.db
M3_SYNC_DBS=$HOME/.m3/engine/agent_memory.db python bin/sync_all.py

# Sync a custom set
M3_SYNC_DBS=$HOME/.m3/engine/agent_memory.db,/data/custom/extra.db python bin/sync_all.py
```

Separate paths with commas or the OS path separator (`:` on POSIX, `;` on Windows). Use absolute paths (your engine root,
`~/.m3/engine` by default): a relative path resolves against the installed
payload, not your engine root.
