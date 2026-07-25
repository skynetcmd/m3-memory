-- pg_046_parity_agent_retention.up.sql
--
-- Brings the PostgreSQL primary schema back into parity with SQLite for the
-- `agent_retention_policies` table.
--
-- The table is created by SQLite migration 010_tier_features.sql but was NEVER
-- mirrored into the PG baseline (pg_primary_v1) or any pg_ migration — the same
-- drift class pg_044 fixed for gdpr_requests. memory_set_retention_impl INSERTs
-- into it and _enforce_retention_policies (the maintenance pass) reads from it;
-- without the table, setting a retention policy raises on PG
-- ("relation agent_retention_policies does not exist") and the maintenance pass's
-- policy enforcement silently no-ops (its SELECT is guarded). So per-agent
-- retention limits / TTLs were entirely non-functional on any PG deployment.
--
-- Caught by tests/test_memory_maintenance_pg_live.py once the maintenance pass
-- was un-guarded on PG; the parity test missed it because the table was absent
-- from that test's _SHARED_CORE_TABLES set (now added, so it can't drift again).
--
-- Type mapping mirrors the SQLite intent (TEXT->TEXT, INTEGER->BIGINT, the
-- SQLite strftime() defaults -> NOW()). CREATE TABLE IF NOT EXISTS keeps this
-- idempotent; migrate_pg wraps the file in one transaction.

CREATE TABLE IF NOT EXISTS agent_retention_policies (
    agent_id        TEXT PRIMARY KEY,
    max_memories    BIGINT DEFAULT 1000,
    ttl_days        BIGINT DEFAULT 0,
    auto_archive    BIGINT DEFAULT 1,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
