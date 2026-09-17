-- 001_bootstrap.up.sql
--
-- Bootstrap the agent DISPATCH store: delivery state for agent-to-agent
-- messaging, in its own SQLite database.
--
-- ── WHY A SEPARATE STORE — THREE LIFECYCLES, NOT THREE TABLES ────────────────
--
-- m3 already splits chat from canonical memory because they are kept
-- differently. Dispatch is a third kind, and the most ephemeral of them:
--
--   agent_memory.db   permanent   durability, FTS, embeddings, GDPR erasure,
--                                 backup, warehouse sync
--   agent_chatlog.db  decaying    bulk ingest, decay, promotion to canonical
--   agent_dispatch.db EPHEMERAL   high write churn, lease reclaim, pruning --
--                                 nothing here is worth backing up
--
-- A delivery record is scaffolding. Once the recipient has acted, the row's
-- only remaining value is a short audit tail; the thing worth keeping is the
-- PAYLOAD, which lives in `memory_items` and which this store merely points at
-- via `memory_id`.
--
-- ⚠ MEASURED, NOT ASSUMED: the rest of m3 ALREADY treats these rows as
-- ephemeral. `pg_sync` replicates memory_items, memory_embeddings and chat_log
-- and has never replicated `notifications`; `gdpr_forget` does not touch them
-- either. Splitting the store makes an existing truth structural instead of
-- incidental.
--
-- Secondary but real: SQLite takes a single write lock for the WHOLE database.
-- Dispatch is the highest-frequency writer in the system -- claim, renew,
-- complete and sweep, per message, per agent -- and in a shared file every one
-- of those contends with ingest, enrichment and embedding. Measured on the
-- claim path: 8 concurrent writers holding 10ms inside a transaction took p99
-- from 74.7ms to 1307ms. In its own store, dispatch contends only with itself.
--
-- ── WHAT THIS FILE DOES NOT DO ───────────────────────────────────────────────
--
-- Nothing is migrated out of `notifications`, which keeps serving every
-- existing caller untouched. Creating a store and moving live mail are
-- different risks and only the second can lose an unread row or clobber a
-- received_at. 047 set that precedent deliberately ("Nothing is backfilled").
--
-- ── STATE IS DERIVED, NEVER STORED ───────────────────────────────────────────
--
-- 047 rejected a `status` column because four states are already derivable and
-- a stored status would SHADOW the live predicates rather than replace them --
-- the first write path that sets one without the other breaks delivery
-- silently. That still holds here. The row is RETAINED on completion and state
-- comes from read_at / failed_at / claimed_by, through the single owners in
-- code (`message_state_sql`, `inbox_membership_sql`,
-- `Dialect.lease_expired_predicate`, `Dialect.lease_fence_predicate`).
--
-- If you are adding a column that answers "is this done", stop: that is the
-- shadowing 047 refused, arriving one store later.

CREATE TABLE IF NOT EXISTS schema_versions (
    version    INTEGER PRIMARY KEY,
    filename   TEXT NOT NULL,
    applied_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notification_dispatch (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,

    -- `memory_id` is a soft reference into memory_items in the MAIN store (no
    -- FK -- it is a different database). The payload lives there because that
    -- table owns FTS and embeddings, so a handoff stays searchable. This row
    -- carries the id, never a copy of the text: one store owns the words, the
    -- other owns the delivery.
    agent_id         TEXT NOT NULL,
    kind             TEXT NOT NULL,
    payload_json     TEXT DEFAULT '{}',
    memory_id        TEXT DEFAULT NULL,

    -- How this message is delivered. Phase one ships agent-to-agent only;
    -- webhook egress is DESIGNED now and built later, arriving as another value
    -- here rather than as a second table. The column exists from the start
    -- because adding a discriminator afterwards means backfilling every row.
    delivery_kind    TEXT NOT NULL DEFAULT 'agent',

    created_at       TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),

    -- Delivery state, DERIVED from these three:
    --   PENDING   = claimed_by IS NULL AND read_at IS NULL AND failed_at IS NULL
    --   CLAIMED   = claimed_by IS NOT NULL AND read_at IS NULL AND failed_at IS NULL
    --   COMPLETED = read_at IS NOT NULL
    --   FAILED    = failed_at IS NOT NULL
    received_at      TEXT DEFAULT NULL,
    read_at          TEXT DEFAULT NULL,
    failed_at        TEXT DEFAULT NULL,

    -- The seven lease columns, matching 047 EXACTLY. The shared primitives
    -- (claim/complete/fail/renew/sweep) all take `table`, so a table carrying
    -- these works with no new code. Extending means "give the new table the
    -- lease columns", never "write new primitives" -- a dispatch-specific
    -- claim would be the §10a duplication defect arriving.
    --
    -- Timestamps come from the DATABASE clock, never a caller's: m3 spans two
    -- hosts via pg_sync, and two agents judging one lease by their own clocks
    -- reach two verdicts.
    claimed_by       TEXT DEFAULT NULL,
    claimed_at       TEXT DEFAULT NULL,
    claim_expires_at TEXT DEFAULT NULL,
    lease_token      TEXT DEFAULT NULL,
    attempt_count    INTEGER NOT NULL DEFAULT 0,

    conversation_id  TEXT DEFAULT NULL,
    reply_to_id      INTEGER DEFAULT NULL
);

-- The waiter's hot path: unread mail for one agent, newest first. Runs per
-- registered agent every few seconds, so it is the query that must not degrade
-- to a scan.
CREATE INDEX IF NOT EXISTS idx_dispatch_agent_unread
    ON notification_dispatch(agent_id, read_at, created_at);

-- The sweeper's only query: expired leases, oldest first. Partial for the same
-- reason as 047's -- a row with no lease is never a sweep candidate and would
-- be dead weight in the index.
--
-- ⚠ The WHERE clause is deliberately a SUPERSET of the sweep predicate
-- (`Dialect.lease_expired_predicate`), which also requires `failed_at IS NULL`.
-- A partial index must be broader than or equal to the query it serves, or the
-- planner cannot use it. Narrowing this to match the predicate exactly would
-- silently drop the index from the plan.
CREATE INDEX IF NOT EXISTS idx_dispatch_expired_lease
    ON notification_dispatch(claim_expires_at)
    WHERE claim_expires_at IS NOT NULL AND read_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_dispatch_conversation
    ON notification_dispatch(conversation_id, id)
    WHERE conversation_id IS NOT NULL;

-- ── deliberation_log: created empty, ON PURPOSE ──────────────────────────────
--
-- The per-attempt egress record for webhook delivery: host, validated record
-- set, pinned IP, verdict, agent id and attempt number. Created now because it
-- is the home the observability design names, and adding it later is a second
-- migration against a live store.
--
-- ⚠ NOTHING WRITES TO THIS TABLE IN PHASE ONE, and that is not an oversight.
-- Agent-to-agent delivery makes no outbound HTTP call, so there is no egress to
-- record. Stated explicitly because an empty audit-shaped table is worse than
-- no table: its emptiness will otherwise be read as proof that nothing
-- happened.
--
-- Two constraints carried from the design, both easy to violate later:
--   * NEVER consulted to decide anything. The moment something reads it to skip
--     a validation, it IS a cached DNS pin and the protection it evidences is
--     gone.
--   * Retention is bounded, so it is incident reconstruction, NOT an audit log
--     of record. Do not cite its contents as evidence of what did not happen.
CREATE TABLE IF NOT EXISTS deliberation_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id      INTEGER NOT NULL,
    attempt_number   INTEGER NOT NULL,
    agent_id         TEXT,
    host             TEXT,
    resolved_records TEXT,
    pinned_ip        TEXT,
    verdict          TEXT,
    detail           TEXT,
    created_at       TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_deliberation_dispatch
    ON deliberation_log(dispatch_id, attempt_number);

-- Retention sweeps read this. Indexed because that sweep is the only query
-- which would otherwise scan the whole table.
CREATE INDEX IF NOT EXISTS idx_deliberation_created
    ON deliberation_log(created_at);
