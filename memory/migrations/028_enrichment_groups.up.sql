-- 028_enrichment_groups.up.sql
--
-- Durable per-group enrichment state for m3_enrich.py.
--
-- Replaces the prior pattern of (a) re-querying every eligible group on every
-- run and (b) ad-hoc retry scripts that compute the unprocessed set externally.
-- Adds resume, dead-letter, crash recovery, cost tracking, and per-obs dedup.
--
-- Two tables:
--   enrichment_runs   — one row per launch (provenance + final counts)
--   enrichment_groups — one row per (source_variant, target_variant, group_key)
--
-- Plus: memory_items.source_group_id column for per-observation dedup linkage.
--
-- Idempotent under the migration runner: bin/migrate_memory.py treats
-- "duplicate column name" / "already exists" errors as a benign re-apply
-- (see migrate_memory.py:505). Direct executescript() will fail on the
-- ALTER TABLE if the column already exists; always go through the runner.

CREATE TABLE IF NOT EXISTS enrichment_runs (
    id              TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    profile         TEXT,
    model           TEXT,
    source_variant  TEXT,
    target_variant  TEXT,
    db_path         TEXT NOT NULL,
    concurrency     INTEGER,
    launch_argv     TEXT,
    host            TEXT,
    git_sha         TEXT,
    status          TEXT NOT NULL,           -- running | completed | aborted | failed
    n_pending       INTEGER NOT NULL DEFAULT 0,
    n_success       INTEGER NOT NULL DEFAULT 0,
    n_failed        INTEGER NOT NULL DEFAULT 0,
    n_empty         INTEGER NOT NULL DEFAULT 0,
    n_dead_letter   INTEGER NOT NULL DEFAULT 0,
    total_cost_usd  REAL NOT NULL DEFAULT 0,
    abort_reason    TEXT,
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_er_started ON enrichment_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_er_status ON enrichment_runs(status);

CREATE TABLE IF NOT EXISTS enrichment_groups (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    source_variant       TEXT NOT NULL,
    target_variant       TEXT NOT NULL,
    group_key            TEXT NOT NULL,
    user_id              TEXT NOT NULL DEFAULT '',
    db_path              TEXT NOT NULL,
    turn_count           INTEGER NOT NULL,
    source_content_hash  TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending',
                         -- pending | in_progress | success | empty | failed | dead_letter | superseded | skipped
    obs_emitted          INTEGER NOT NULL DEFAULT 0,
    attempts             INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    error_class          TEXT,
    enrichment_ms        INTEGER,
    tokens_in            INTEGER,
    tokens_out           INTEGER,
    cost_usd             REAL,
    claim_token          TEXT,
    claimed_at           TEXT,
    next_eligible_at     TEXT,
    first_attempt_at     TEXT,
    last_attempt_at      TEXT,
    profile              TEXT,
    model                TEXT,
    enrich_run_id        TEXT,
    created_at           TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_variant, target_variant, group_key)
);

CREATE INDEX IF NOT EXISTS idx_eg_status_variant
    ON enrichment_groups(status, source_variant, target_variant);
CREATE INDEX IF NOT EXISTS idx_eg_run
    ON enrichment_groups(enrich_run_id);
CREATE INDEX IF NOT EXISTS idx_eg_eligible
    ON enrichment_groups(status, next_eligible_at);
CREATE INDEX IF NOT EXISTS idx_eg_claim
    ON enrichment_groups(status, claimed_at);

-- Per-observation dedup: links each emitted observation back to the source
-- group that produced it. Crash recovery can resume mid-conversation by
-- skipping (source_group_id, content_hash) pairs that already exist.
ALTER TABLE memory_items ADD COLUMN source_group_id INTEGER;

CREATE INDEX IF NOT EXISTS idx_mi_source_group
    ON memory_items(source_group_id) WHERE source_group_id IS NOT NULL;
