-- pg_045_wiki_cluster_run.up.sql
--
-- PostgreSQL mirror of SQLite migration 041_wiki_cluster_run.up.sql — wiki
-- compile provenance (cluster_run) + membership cache (cluster_members), plan G1
-- (~/.m3-private/plans/WIKI_SYNTHESIS_MEMORY_TYPE.md). Keeps the PG primary
-- schema in parity with SQLite (gated by tests/test_schema_parity_pg_live.py).
--
-- Type mapping mirrors the SQLite intent: TEXT->TEXT, INTEGER->BIGINT,
-- REAL->DOUBLE PRECISION, the SQLite strftime() default -> NOW()/TIMESTAMPTZ.
-- CREATE TABLE / CREATE INDEX IF NOT EXISTS keep this idempotent; migrate_pg
-- wraps the file in one transaction.
--
-- See the SQLite migration for the full design rationale (provenance vs cache,
-- frozen-membership correctness payoff, GDPR G2 erasure record). `use_networkx`
-- is intentionally NOT a column: networkx is a base dependency now, so clustering
-- is always-networkx (a constant, not a reproducibility parameter).

CREATE TABLE IF NOT EXISTS cluster_run (
    id                   TEXT PRIMARY KEY,
    seq                  BIGINT NOT NULL,
    completed_at         TIMESTAMPTZ,
    as_of                TIMESTAMPTZ,
    scope                TEXT,
    user_id              TEXT,
    importance_threshold DOUBLE PRECISION,
    entity_comention     BIGINT DEFAULT 0,
    entity_max_degree    BIGINT,
    cluster_count        BIGINT DEFAULT 0,
    erased_member_ids    TEXT,
    erased_count         BIGINT DEFAULT 0,
    first_erased_at      TIMESTAMPTZ,
    last_erased_at       TIMESTAMPTZ,
    membership_flushed   BIGINT DEFAULT 0,
    page_hash_digest     TEXT,
    created_at           TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cluster_run_seq ON cluster_run(seq);
CREATE INDEX IF NOT EXISTS idx_cluster_run_completed
    ON cluster_run(completed_at) WHERE completed_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS cluster_members (
    run_id            TEXT NOT NULL,
    cluster_key       TEXT NOT NULL,
    memory_id         TEXT NOT NULL,
    cluster_hash      TEXT,
    head_synthesis_id TEXT,
    PRIMARY KEY (run_id, cluster_key, memory_id)
);

CREATE INDEX IF NOT EXISTS idx_cluster_members_memory ON cluster_members(memory_id);
CREATE INDEX IF NOT EXISTS idx_cluster_members_run ON cluster_members(run_id);
CREATE INDEX IF NOT EXISTS idx_cluster_members_hash ON cluster_members(cluster_hash);
