-- pg_001_bootstrap.up.sql
--
-- PostgreSQL counterpart of 001_bootstrap.up.sql: the agent DISPATCH store.
-- Same intent, native types (BIGINT IDENTITY, JSONB, TIMESTAMPTZ), and the
-- same three-lifecycle rationale -- see the SQLite header for the long form.
--
-- ── ISOLATION IS A SCHEMA HERE, NOT A SEPARATE DATABASE ──────────────────────
--
-- On SQLite the dispatch store is a separate FILE, because SQLite's unit of
-- write contention and of backup is the file. PostgreSQL has neither problem:
-- there is no database-wide write lock, and a schema is already the unit of
-- permissions, `search_path` and selective dump/restore.
--
-- So dispatch gets its own SCHEMA in the SAME database. That buys the isolation
-- the split is for -- separate grants, separate backup selection, no accidental
-- joins to canonical memory -- WITHOUT a second connection URL, a second pool,
-- or a second thing to configure and get wrong. Several m3 fleets can share one
-- server by giving each its own schema name.
--
-- ⚠ THE SCHEMA IS NOT HARDCODED. It is created by the caller, which sets
-- `search_path` before running this file; the DDL below is deliberately
-- unqualified so the same script serves any schema name. Do not "fix" that by
-- writing `m3_dispatch.notification_dispatch` here -- that would hardcode one
-- fleet's layout into shipped DDL.
--
-- ⚠ PostgreSQL is NOT optional for multi-node. Single-node multi-agent runs on
-- SQLite, but MULTI-NODE REQUIRES PostgreSQL, and that is mechanism rather than
-- preference: SQLite arrival detection watches the WAL file's mtime+size, a
-- filesystem signal, and a WAL on machine A cannot wake a waiter on machine B.
-- The PG path polls the shared table, so it crosses hosts.
--
-- Nothing is migrated out of `notifications`, which keeps serving every
-- existing caller untouched. Timestamps are written from the DATABASE clock
-- (NOW()), never a caller's: pg_sync spans two hosts, so an N-clock expiry
-- comparison is a correctness bug.

CREATE TABLE IF NOT EXISTS schema_versions (
    version    INTEGER PRIMARY KEY,
    filename   TEXT NOT NULL,
    applied_at TIMESTAMPTZ DEFAULT NOW()
);

--
-- ── WHY IDS START AT 1,000,000,000 ───────────────────────────────────────────
--
-- `notifications` and `notification_dispatch` are different tables in
-- different stores, and both would otherwise autoincrement from 1. Every id
-- would then exist twice, and `notifications_ack_impl(id)` takes a BARE
-- INTEGER -- so an ack could close the wrong message, and a waiter comparing a
-- set of ids would treat two distinct messages as one. Measured while building
-- the split: a cross-store read returned [1, 1] for two different rows.
--
-- The floor makes ids globally distinguishable without changing the tool
-- surface: no "dispatch:1" parsing for callers, no UUID migration.
--
-- WHY THIS NUMBER. The main store is at max_id 1272 after five months and
-- writes ~722 notifications/week on this box. A 1,001 offset collides
-- IMMEDIATELY; a billion is ~26,000 years of headroom at that rate. The
-- constant is deliberately far past any plausible volume rather than tuned
-- close to it.
--
-- ⚠ THE CHECK IS THE POINT, not the seeded sequence. A gap someone must
-- remember to preserve is not a guarantee -- the first hand-written INSERT with
-- an explicit id breaks it silently. The constraint makes the database refuse
-- a colliding id, so the property is enforced rather than hoped for (§3:
-- prefer the mechanism that makes the lie impossible over the docstring that
-- forbids it).

CREATE TABLE IF NOT EXISTS notification_dispatch (
    id               BIGINT GENERATED ALWAYS AS IDENTITY
                     (START WITH 1000000000) PRIMARY KEY
                     CHECK (id >= 1000000000),

    -- `memory_id` is a soft reference into memory_items, which lives in the
    -- MAIN schema (no FK -- crossing schemas with a constraint would couple the
    -- two lifecycles this split exists to separate). The payload lives there
    -- because that table owns FTS and embeddings; this row carries the id.
    agent_id         TEXT NOT NULL,
    kind             TEXT NOT NULL,
    payload_json     JSONB DEFAULT '{}',
    memory_id        TEXT DEFAULT NULL,

    -- Phase one ships agent-to-agent only; webhook egress is DESIGNED now and
    -- built later, arriving as another value here rather than a second table.
    delivery_kind    TEXT NOT NULL DEFAULT 'agent',

    created_at       TIMESTAMPTZ DEFAULT NOW(),

    -- Delivery state, DERIVED from these three -- never a status string. A
    -- stored status would shadow the live predicates rather than replace them,
    -- which is what 047 refused.
    received_at      TIMESTAMPTZ DEFAULT NULL,
    read_at          TIMESTAMPTZ DEFAULT NULL,
    failed_at        TIMESTAMPTZ DEFAULT NULL,

    -- The seven lease columns, matching pg_056 exactly, so the shared
    -- primitives (claim/complete/fail/renew/sweep -- all of which take `table`)
    -- work here with no new code and no per-table branch.
    claimed_by       TEXT DEFAULT NULL,
    claimed_at       TIMESTAMPTZ DEFAULT NULL,
    claim_expires_at TIMESTAMPTZ DEFAULT NULL,
    lease_token      TEXT DEFAULT NULL,
    attempt_count    INTEGER NOT NULL DEFAULT 0,

    conversation_id  TEXT DEFAULT NULL,
    reply_to_id      BIGINT DEFAULT NULL
);

-- The waiter's hot path. On PG this is what pg_child_loop runs per agent per
-- interval, so it is the query that must not degrade to a scan.
CREATE INDEX IF NOT EXISTS idx_dispatch_agent_unread
    ON notification_dispatch(agent_id, read_at, created_at);

-- The sweeper's only query. Deliberately a SUPERSET of the sweep predicate,
-- which also requires `failed_at IS NULL`: a partial index must be broader than
-- or equal to the query it serves or the planner cannot use it.
CREATE INDEX IF NOT EXISTS idx_dispatch_expired_lease
    ON notification_dispatch(claim_expires_at)
    WHERE claim_expires_at IS NOT NULL AND read_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_dispatch_conversation
    ON notification_dispatch(conversation_id, id)
    WHERE conversation_id IS NOT NULL;

-- ── deliberation_log: created empty, ON PURPOSE ──────────────────────────────
--
-- The per-attempt egress record for webhook delivery. Created now because it is
-- the home the observability design names; adding it later is a second
-- migration against a live store.
--
-- ⚠ NOTHING WRITES TO THIS TABLE IN PHASE ONE. Agent-to-agent delivery makes no
-- outbound HTTP call, so there is no egress to record. Stated explicitly
-- because an empty audit-shaped table is worse than no table: its emptiness
-- will otherwise be read as proof that nothing happened.
--
--   * NEVER consulted to decide anything. The moment something reads it to skip
--     a validation, it IS a cached DNS pin.
--   * Retention is bounded, so it is incident reconstruction, NOT an audit log
--     of record.
CREATE TABLE IF NOT EXISTS deliberation_log (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dispatch_id      BIGINT NOT NULL,
    attempt_number   INTEGER NOT NULL,
    agent_id         TEXT,
    host             TEXT,
    resolved_records JSONB,
    pinned_ip        TEXT,
    verdict          TEXT,
    detail           TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_deliberation_dispatch
    ON deliberation_log(dispatch_id, attempt_number);

CREATE INDEX IF NOT EXISTS idx_deliberation_created
    ON deliberation_log(created_at);
