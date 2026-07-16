-- pg_040_observer_enrich_tables.up.sql
--
-- First PG-native incremental migration (applied by bin/migrate_pg.py after the
-- v39 baseline). Adds the observer/reflector queues (SQLite migration 025) and
-- the enrichment run/group state tables (SQLite migration 028), which the ported
-- run_observer.py and m3_enrich.py read/write on a PostgreSQL primary store.
--
-- Translated from the SQLite DDL: AUTOINCREMENT -> GENERATED ALWAYS AS IDENTITY,
-- TEXT timestamps -> TIMESTAMPTZ, REAL -> DOUBLE PRECISION. The
-- memory_items.source_group_id column + its partial index already exist in the
-- v39 baseline (pg_primary_v1.sql), so migration 028's ALTER is NOT repeated here.
--
-- One implicit transaction (no explicit BEGIN/COMMIT) — migrate_pg.py wraps the
-- whole file. Idempotent: CREATE ... IF NOT EXISTS throughout.

CREATE TABLE IF NOT EXISTS observation_queue (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    user_id         TEXT,
    enqueued_at     TIMESTAMPTZ DEFAULT NOW(),
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT,
    last_attempt_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_oq_conversation_id ON observation_queue(conversation_id);
CREATE INDEX IF NOT EXISTS idx_oq_attempts ON observation_queue(attempts, enqueued_at);

CREATE TABLE IF NOT EXISTS reflector_queue (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id      TEXT NOT NULL,
    user_id              TEXT,
    obs_count_at_enqueue INTEGER,
    enqueued_at          TIMESTAMPTZ DEFAULT NOW(),
    attempts             INTEGER DEFAULT 0,
    last_error           TEXT,
    last_attempt_at      TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rq_user_conv ON reflector_queue(user_id, conversation_id);
CREATE INDEX IF NOT EXISTS idx_rq_attempts ON reflector_queue(attempts, enqueued_at);

CREATE TABLE IF NOT EXISTS enrichment_runs (
    id              TEXT PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ,
    profile         TEXT,
    model           TEXT,
    source_variant  TEXT,
    target_variant  TEXT,
    db_path         TEXT NOT NULL,
    concurrency     INTEGER,
    launch_argv     TEXT,
    host            TEXT,
    git_sha         TEXT,
    status          TEXT NOT NULL,
    n_pending       INTEGER NOT NULL DEFAULT 0,
    n_success       INTEGER NOT NULL DEFAULT 0,
    n_failed        INTEGER NOT NULL DEFAULT 0,
    n_empty         INTEGER NOT NULL DEFAULT 0,
    n_dead_letter   INTEGER NOT NULL DEFAULT 0,
    total_cost_usd  DOUBLE PRECISION NOT NULL DEFAULT 0,
    abort_reason    TEXT,
    notes           TEXT
);
CREATE INDEX IF NOT EXISTS idx_erun_started ON enrichment_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_erun_status  ON enrichment_runs(status);

CREATE TABLE IF NOT EXISTS enrichment_groups (
    id                   BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_variant       TEXT NOT NULL,
    target_variant       TEXT NOT NULL,
    group_key            TEXT NOT NULL,
    user_id              TEXT NOT NULL DEFAULT '',
    db_path              TEXT NOT NULL,
    turn_count           INTEGER NOT NULL,
    source_content_hash  TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending',
    obs_emitted          INTEGER NOT NULL DEFAULT 0,
    attempts             INTEGER NOT NULL DEFAULT 0,
    last_error           TEXT,
    error_class          TEXT,
    enrichment_ms        INTEGER,
    tokens_in            INTEGER,
    tokens_out           INTEGER,
    cost_usd             DOUBLE PRECISION,
    claim_token          TEXT,
    claimed_at           TIMESTAMPTZ,
    next_eligible_at     TIMESTAMPTZ,
    first_attempt_at     TIMESTAMPTZ,
    last_attempt_at      TIMESTAMPTZ,
    profile              TEXT,
    model                TEXT,
    enrich_run_id        TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(source_variant, target_variant, group_key)
);
CREATE INDEX IF NOT EXISTS idx_eg_status_variant
    ON enrichment_groups(status, source_variant, target_variant);
CREATE INDEX IF NOT EXISTS idx_eg_run    ON enrichment_groups(enrich_run_id);
CREATE INDEX IF NOT EXISTS idx_eg_eligible ON enrichment_groups(status, next_eligible_at);
CREATE INDEX IF NOT EXISTS idx_eg_claim  ON enrichment_groups(status, claimed_at);
