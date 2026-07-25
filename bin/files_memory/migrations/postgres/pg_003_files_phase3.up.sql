-- pg_003_files_phase3.up.sql  (PostgreSQL)
-- Mirror of SQLite 003_files_phase3.up.sql. Follows pg_002.
-- fact_hit_stats + semantic_dedup_candidates. Type mapping per pg_001's header.
-- The partial index (WHERE reviewed_at IS NULL) is supported identically on PG.

CREATE TABLE IF NOT EXISTS files.fact_hit_stats (
    fact_uuid    TEXT PRIMARY KEY REFERENCES files.facts(uuid) ON DELETE CASCADE,
    hit_count    BIGINT NOT NULL DEFAULT 0,
    first_hit_at TEXT,
    last_hit_at  TEXT
);

CREATE TABLE IF NOT EXISTS files.semantic_dedup_candidates (
    uuid          TEXT PRIMARY KEY,
    leaf_a        TEXT NOT NULL REFERENCES files.leaves(uuid) ON DELETE CASCADE,
    leaf_b        TEXT NOT NULL REFERENCES files.leaves(uuid) ON DELETE CASCADE,
    cosine        DOUBLE PRECISION NOT NULL,
    detected_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at   TEXT,
    review_action TEXT,
    metadata      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_dedup_pair ON files.semantic_dedup_candidates(leaf_a, leaf_b);
CREATE INDEX IF NOT EXISTS idx_dedup_unreviewed ON files.semantic_dedup_candidates(reviewed_at)
    WHERE reviewed_at IS NULL;
