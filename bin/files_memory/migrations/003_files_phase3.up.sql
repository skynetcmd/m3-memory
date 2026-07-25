-- 003_files_phase3.up.sql  (SQLite)
--
-- files_memory phase-3 additions (fact_hit_stats, semantic_dedup_candidates).
-- Generated verbatim from schema.SCHEMA_V3. Follows 002.
--
-- The runner wraps this file in one transaction; no BEGIN/COMMIT here.
-- Connection-level PRAGMAs (journal_mode etc.) are applied by the
-- connection layer, not here. Version tracking is the runner's job, so
-- the schema_migrations bookkeeping from the blob is intentionally omitted.

-- fact_hit_stats: rolling usage counters for promotion suggestions.
-- One row per fact (1:1 with facts); incremented when files_search
-- returns the fact's leaf. Promotability_score = hit_count * confidence
-- * recency_factor (computed in code, not stored — recency depends on
-- query time).
CREATE TABLE IF NOT EXISTS fact_hit_stats (
    fact_uuid    TEXT PRIMARY KEY REFERENCES facts(uuid) ON DELETE CASCADE,
    hit_count    INTEGER NOT NULL DEFAULT 0,
    first_hit_at TEXT,
    last_hit_at  TEXT
);

-- semantic_dedup_candidates: phase-3.3 output. Pairs of leaves whose
-- embedding cosine exceeds threshold. Populated by files_dedup; consumed
-- by an (eventually interactive) review tool.
CREATE TABLE IF NOT EXISTS semantic_dedup_candidates (
    uuid          TEXT PRIMARY KEY,
    leaf_a        TEXT NOT NULL REFERENCES leaves(uuid) ON DELETE CASCADE,
    leaf_b        TEXT NOT NULL REFERENCES leaves(uuid) ON DELETE CASCADE,
    cosine        REAL NOT NULL,
    detected_at   TEXT NOT NULL DEFAULT (datetime('now')),
    reviewed_at   TEXT,
    review_action TEXT,              -- 'kept'|'merged'|'ignored'
    metadata      TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_dedup_pair ON semantic_dedup_candidates(leaf_a, leaf_b);
CREATE INDEX IF NOT EXISTS idx_dedup_unreviewed ON semantic_dedup_candidates(reviewed_at)
    WHERE reviewed_at IS NULL;
