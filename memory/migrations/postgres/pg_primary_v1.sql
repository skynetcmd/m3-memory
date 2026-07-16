-- m3-memory PostgreSQL PRIMARY schema (pg_primary_v1.sql)
--
-- This is the PRIMARY-backend schema, NOT the warehouse mirror
-- (see pg_warehouse_chatlog_v1.sql for that, kept separate and namespaced
-- under m3_warehouse). This file is generated to match the live SQLite
-- primary schema 1:1 (see memory/migrations, live_schema.sql ground truth)
-- for the Phase 1 selectable-backend work (SQLite vs Postgres as primary
-- store). Tables are created in the default `public` search_path — no
-- custom schema/namespace.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS
-- throughout. Safe to re-run.
--
-- Apply via psql:
--   psql -h <host> -U <user> -d <database> -f pg_primary_v1.sql

BEGIN;

-- =====================================================
-- memory_items
-- =====================================================

CREATE TABLE IF NOT EXISTS memory_items (
    id                   TEXT PRIMARY KEY,
    type                 TEXT NOT NULL,
    title                TEXT,
    content              TEXT,
    metadata_json        JSONB,
    agent_id             TEXT,
    model_id             TEXT,
    change_agent         TEXT DEFAULT 'unknown',
    importance           DOUBLE PRECISION DEFAULT 0.5,
    source               TEXT DEFAULT 'agent',
    origin_device        TEXT DEFAULT 'macbook',
    is_deleted           INTEGER DEFAULT 0,
    expires_at           TIMESTAMPTZ,
    decay_rate           DOUBLE PRECISION DEFAULT 0.0,
    created_at           TIMESTAMPTZ DEFAULT NOW(),
    updated_at           TIMESTAMPTZ,
    last_accessed_at     TIMESTAMPTZ,
    access_count         BIGINT DEFAULT 0,
    user_id              TEXT DEFAULT '',
    scope                TEXT DEFAULT 'agent',
    valid_from           TIMESTAMPTZ DEFAULT NULL,
    valid_to             TIMESTAMPTZ DEFAULT NULL,
    content_hash         TEXT DEFAULT '',
    read_at              TIMESTAMPTZ DEFAULT NULL,
    conversation_id      TEXT,
    refresh_on           TIMESTAMPTZ,
    refresh_reason       TEXT,
    variant              TEXT DEFAULT NULL,
    source_group_id      INTEGER,
    stage1_kg_done       INTEGER DEFAULT 0,
    confidence           DOUBLE PRECISION DEFAULT NULL,
    belief_alpha         DOUBLE PRECISION DEFAULT NULL,
    belief_beta          DOUBLE PRECISION DEFAULT NULL,
    corroboration_count  BIGINT DEFAULT 0,
    contradiction_count  BIGINT DEFAULT 0,
    pinned               INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_mi_type       ON memory_items(type);
CREATE INDEX IF NOT EXISTS idx_mi_agent      ON memory_items(agent_id);
CREATE INDEX IF NOT EXISTS idx_mi_model      ON memory_items(model_id);
CREATE INDEX IF NOT EXISTS idx_mi_created    ON memory_items(created_at);
CREATE INDEX IF NOT EXISTS idx_mi_deleted    ON memory_items(is_deleted);
CREATE INDEX IF NOT EXISTS idx_mi_deleted_type ON memory_items(is_deleted, type);
CREATE INDEX IF NOT EXISTS idx_mi_importance   ON memory_items(importance);
CREATE INDEX IF NOT EXISTS idx_mi_updated      ON memory_items(updated_at);
CREATE INDEX IF NOT EXISTS idx_mi_change_agent ON memory_items(change_agent);
CREATE INDEX IF NOT EXISTS idx_mi_user_id ON memory_items(user_id);
CREATE INDEX IF NOT EXISTS idx_mi_scope ON memory_items(scope);
CREATE INDEX IF NOT EXISTS idx_mi_valid_from ON memory_items(valid_from);
CREATE INDEX IF NOT EXISTS idx_mi_handoff_inbox
    ON memory_items(agent_id, type, read_at, created_at);
CREATE INDEX IF NOT EXISTS idx_mi_refresh_on
    ON memory_items(refresh_on)
    WHERE refresh_on IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mi_conversation_id
    ON memory_items(conversation_id, created_at)
    WHERE is_deleted = 0;

-- Full-text search (the tsvector analogue of SQLite's memory_items_fts / bm25).
-- A GENERATED column keeps the search vector in sync automatically — no triggers
-- (the FTS5 side needs external-content triggers; Postgres does it declaratively).
-- title is weighted 'A' and content 'B' so a title hit outranks a body hit,
-- mirroring the intent of TITLE_MATCH_BOOST on the SQLite side. Queried with
-- `search_vector @@ tsquery` and ranked with ts_rank (see PostgresBackend
-- .keyword_search). GIN index makes @@ index-accelerated.
ALTER TABLE memory_items
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(content, '')), 'B')
    ) STORED;
CREATE INDEX IF NOT EXISTS idx_mi_search_vector
    ON memory_items USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS idx_mi_active_type_created
    ON memory_items(is_deleted, type, created_at DESC)
    WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_mi_scope_type
    ON memory_items(scope, type)
    WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_mi_user_agent
    ON memory_items(user_id, agent_id);
CREATE INDEX IF NOT EXISTS idx_mi_content_hash
    ON memory_items(content_hash)
    WHERE content_hash != '';
CREATE INDEX IF NOT EXISTS idx_mi_source ON memory_items(source);
CREATE INDEX IF NOT EXISTS idx_memory_items_variant
    ON memory_items(variant) WHERE variant IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_items_user_variant
    ON memory_items(user_id, variant) WHERE variant IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_items_chat_log
    ON memory_items (conversation_id, created_at)
    WHERE type='chat_log' AND is_deleted=0;

-- NOTE: source used json_extract(metadata_json,'$.host_agent') etc against
-- SQLite TEXT; metadata_json is now JSONB, translated to ->> expression indexes.
CREATE INDEX IF NOT EXISTS idx_memory_items_host_agent
    ON memory_items ((metadata_json->>'host_agent'))
    WHERE type='chat_log';
CREATE INDEX IF NOT EXISTS idx_memory_items_provider
    ON memory_items ((metadata_json->>'provider'))
    WHERE type='chat_log';
CREATE INDEX IF NOT EXISTS idx_memory_items_model_id
    ON memory_items ((metadata_json->>'model_id'))
    WHERE type='chat_log';
CREATE INDEX IF NOT EXISTS idx_memory_items_provider_time
    ON memory_items ((metadata_json->>'provider'), created_at)
    WHERE type='chat_log' AND is_deleted=0;
CREATE INDEX IF NOT EXISTS idx_mi_type_user_obs
  ON memory_items(type, user_id, valid_from)
  WHERE type='observation';
CREATE INDEX IF NOT EXISTS idx_mi_id_prefix8
    ON memory_items(SUBSTR(id, 1, 8));
CREATE INDEX IF NOT EXISTS idx_mi_source_group
    ON memory_items(source_group_id) WHERE source_group_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mi_stage1_kg_pending
    ON memory_items(variant, type)
    WHERE stage1_kg_done = 0;
CREATE INDEX IF NOT EXISTS idx_memory_items_confidence
    ON memory_items(confidence) WHERE confidence IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_items_pinned
    ON memory_items(pinned) WHERE pinned = 1;

-- Note: SQLite FTS5 virtual tables (memory_items_fts, memory_items_fts_config,
-- memory_items_fts_data, memory_items_fts_docsize, memory_items_fts_idx) are
-- NOT translated here. Postgres full-text search uses native tsvector /
-- GIN indexes, to be added separately (not part of this mechanical DDL pass).

-- =====================================================
-- memory_embeddings
-- =====================================================

CREATE TABLE IF NOT EXISTS memory_embeddings (
    id          TEXT PRIMARY KEY,
    memory_id   TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    embedding   BYTEA NOT NULL,
    -- NOTE: default embed_model is historically inaccurate; live data uses
    -- text-embedding-bge-m3. Do not rely on this default.
    embed_model TEXT DEFAULT 'jina-embeddings-v5',
    dim         BIGINT DEFAULT 1024,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    content_hash TEXT,
    vector_kind TEXT NOT NULL DEFAULT 'default'
);

CREATE INDEX IF NOT EXISTS idx_me_memory_id  ON memory_embeddings(memory_id);
CREATE INDEX IF NOT EXISTS idx_me_memory_model
    ON memory_embeddings(memory_id, embed_model);
CREATE INDEX IF NOT EXISTS idx_me_content_hash_model
    ON memory_embeddings(content_hash, embed_model);
CREATE INDEX IF NOT EXISTS idx_me_memory_kind
    ON memory_embeddings(memory_id, vector_kind);

-- =====================================================
-- memory_relationships
-- =====================================================

CREATE TABLE IF NOT EXISTS memory_relationships (
    id                TEXT PRIMARY KEY,
    from_id           TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    to_id             TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    weight            DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_mr_from ON memory_relationships(from_id);
CREATE INDEX IF NOT EXISTS idx_mr_to   ON memory_relationships(to_id);
CREATE INDEX IF NOT EXISTS idx_mr_rel_type ON memory_relationships(relationship_type);
-- Unique edge (SQLite migration 039): the arbiter for memory_link_impl's
-- idempotent ON CONFLICT (from_id, to_id, relationship_type) DO NOTHING.
CREATE UNIQUE INDEX IF NOT EXISTS idx_mr_unique_edge
    ON memory_relationships(from_id, to_id, relationship_type);

-- =====================================================
-- memory_history (audit trail — the supersede/create path writes here)
-- =====================================================

CREATE TABLE IF NOT EXISTS memory_history (
    id          TEXT PRIMARY KEY,
    memory_id   TEXT NOT NULL,
    event       TEXT NOT NULL,
    prev_value  TEXT,
    new_value   TEXT,
    field       TEXT DEFAULT 'content',
    actor_id    TEXT DEFAULT '',
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_mh_memory_id ON memory_history(memory_id);
CREATE INDEX IF NOT EXISTS idx_mh_created ON memory_history(created_at);

-- =====================================================
-- chroma_sync_queue (L3 mirror sync queue)
-- =====================================================

CREATE TABLE IF NOT EXISTS chroma_sync_queue (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    memory_id     TEXT NOT NULL,
    operation     TEXT NOT NULL,
    attempts      BIGINT DEFAULT 0,
    stalled_since TIMESTAMPTZ,
    queued_at     TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_csq_memory_id ON chroma_sync_queue(memory_id);

-- =====================================================
-- agents (agent registry; trust ledger references it)
-- =====================================================

CREATE TABLE IF NOT EXISTS agents (
    agent_id       TEXT PRIMARY KEY,
    role           TEXT DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'active',
    capabilities   TEXT DEFAULT '[]',
    metadata_json  JSONB DEFAULT '{}',
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    last_seen      TIMESTAMPTZ DEFAULT NOW(),
    trust_score    DOUBLE PRECISION DEFAULT 1.0
);

-- =====================================================
-- memory_corroborations (trust/corroboration ledger)
-- =====================================================

CREATE TABLE IF NOT EXISTS memory_corroborations (
    id             TEXT PRIMARY KEY,
    memory_id      TEXT NOT NULL,
    source_kind    TEXT NOT NULL DEFAULT 'agent',
    source_ref     TEXT NOT NULL DEFAULT '',
    trust_at_write DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    delta          DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at     TIMESTAMPTZ DEFAULT NOW()
);
-- Partial unique index = the dedup arbiter for trust.py's ON CONFLICT (a source
-- corroborating the same memory twice, with a positive delta, is a no-op).
CREATE UNIQUE INDEX IF NOT EXISTS idx_corrob_memory_source
    ON memory_corroborations(memory_id, source_kind, source_ref)
    WHERE delta > 0;

-- =====================================================
-- entities + entity graph (SQLite migration 024)
-- =====================================================
-- The entity-relation graph: canonical entities, memory<->entity mention links,
-- typed entity<->entity edges, and the extraction work queue. Translated from
-- 024_entity_graph.up.sql — AUTOINCREMENT -> GENERATED ALWAYS AS IDENTITY,
-- TEXT timestamps -> TIMESTAMPTZ, TEXT '{}' -> JSONB, REAL -> DOUBLE PRECISION.

CREATE TABLE IF NOT EXISTS entities (
    id              TEXT PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    attributes_json JSONB DEFAULT '{}',
    valid_from      TIMESTAMPTZ,
    valid_to        TIMESTAMPTZ,
    content_hash    TEXT,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_entities_canonical_type ON entities(canonical_name, entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_type           ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_hash           ON entities(content_hash);

CREATE TABLE IF NOT EXISTS memory_item_entities (
    memory_id       TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    mention_text    TEXT,
    mention_offset  INTEGER DEFAULT 0,
    confidence      DOUBLE PRECISION DEFAULT 0.85,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (memory_id, entity_id, mention_offset),
    FOREIGN KEY (memory_id) REFERENCES memory_items(id) ON DELETE CASCADE,
    FOREIGN KEY (entity_id) REFERENCES entities(id)     ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mie_entity ON memory_item_entities(entity_id);

CREATE TABLE IF NOT EXISTS entity_relationships (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_entity      TEXT NOT NULL,
    to_entity        TEXT NOT NULL,
    predicate        TEXT NOT NULL,
    confidence       DOUBLE PRECISION DEFAULT 0.85,
    valid_from       TIMESTAMPTZ,
    valid_to         TIMESTAMPTZ,
    source_memory_id TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    FOREIGN KEY (from_entity)      REFERENCES entities(id) ON DELETE CASCADE,
    FOREIGN KEY (to_entity)        REFERENCES entities(id) ON DELETE CASCADE,
    FOREIGN KEY (source_memory_id) REFERENCES memory_items(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_er_from      ON entity_relationships(from_entity, predicate);
CREATE INDEX IF NOT EXISTS idx_er_to        ON entity_relationships(to_entity, predicate);
CREATE INDEX IF NOT EXISTS idx_er_predicate ON entity_relationships(predicate);

CREATE TABLE IF NOT EXISTS entity_extraction_queue (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    memory_id       TEXT NOT NULL,
    enqueued_at     TIMESTAMPTZ DEFAULT NOW(),
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT,
    last_attempt_at TIMESTAMPTZ,
    FOREIGN KEY (memory_id) REFERENCES memory_items(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_eeq_memory_id ON entity_extraction_queue(memory_id);
CREATE INDEX IF NOT EXISTS idx_eeq_attempts ON entity_extraction_queue(attempts, enqueued_at);

-- =====================================================
-- entity_embeddings (SQLite migration 032)
-- =====================================================
-- Store-once entity-name vectors for Tier-3 cosine resolution. BLOB -> BYTEA
-- (packed float32, scored by the DB-blind Rust cosine, same as memory_embeddings).

CREATE TABLE IF NOT EXISTS entity_embeddings (
    entity_id   TEXT PRIMARY KEY REFERENCES entities(id) ON DELETE CASCADE,
    embedding   BYTEA NOT NULL,
    embed_model TEXT,
    dim         INTEGER,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- =====================================================
-- fact_enrichment_queue (SQLite migration 023)
-- =====================================================

CREATE TABLE IF NOT EXISTS fact_enrichment_queue (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    memory_id       TEXT NOT NULL,
    enqueued_at     TIMESTAMPTZ DEFAULT NOW(),
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT,
    last_attempt_at TIMESTAMPTZ,
    FOREIGN KEY (memory_id) REFERENCES memory_items(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_feq_memory_id ON fact_enrichment_queue(memory_id);
CREATE INDEX IF NOT EXISTS idx_feq_attempts ON fact_enrichment_queue(attempts, enqueued_at);

-- =====================================================
-- notifications + tasks (SQLite migration 012 orchestration)
-- =====================================================
-- (The agents table from 012 already exists above with a superset of columns —
-- role/status/capabilities/last_seen plus trust_score — so it is not repeated.)
-- notifications.id is read back via RETURNING on PG (no last_insert_rowid()).

CREATE TABLE IF NOT EXISTS notifications (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    agent_id       TEXT NOT NULL,
    kind           TEXT NOT NULL,
    payload_json   JSONB DEFAULT '{}',
    created_at     TIMESTAMPTZ DEFAULT NOW(),
    read_at        TIMESTAMPTZ DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_notif_agent_unread
    ON notifications(agent_id, read_at, created_at);
CREATE INDEX IF NOT EXISTS idx_notif_agent_kind
    ON notifications(agent_id, kind, read_at);

CREATE TABLE IF NOT EXISTS tasks (
    id                 TEXT PRIMARY KEY,
    title              TEXT NOT NULL,
    description        TEXT DEFAULT '',
    state              TEXT NOT NULL DEFAULT 'pending',
    owner_agent        TEXT DEFAULT NULL,
    created_by         TEXT NOT NULL,
    parent_task_id     TEXT DEFAULT NULL,
    result_memory_id   TEXT DEFAULT NULL,
    metadata_json      JSONB DEFAULT '{}',
    created_at         TIMESTAMPTZ DEFAULT NOW(),
    updated_at         TIMESTAMPTZ DEFAULT NOW(),
    completed_at       TIMESTAMPTZ DEFAULT NULL,
    deleted_at         TIMESTAMPTZ DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_owner_state ON tasks(owner_agent, state);
CREATE INDEX IF NOT EXISTS idx_tasks_parent      ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_state       ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_created_by  ON tasks(created_by);
CREATE INDEX IF NOT EXISTS idx_tasks_deleted_at  ON tasks(deleted_at);

-- =====================================================
-- bypass_surface (SQLite migration 033)
-- =====================================================
-- Materialized rank-independent recall surface (ADR-0001). Rebuildable from
-- entities + observations via bin/memory/entity.build_bypass_surface().

CREATE TABLE IF NOT EXISTS bypass_surface (
    conversation_id TEXT NOT NULL,
    memory_id       TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    source          TEXT NOT NULL,
    strategy        TEXT,
    user_id         TEXT,
    scope           TEXT NOT NULL DEFAULT 'agent',
    cap             INTEGER,
    built_at        TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (conversation_id, memory_id)
);
CREATE INDEX IF NOT EXISTS idx_bypass_surface_scope
    ON bypass_surface(conversation_id, scope, user_id);

-- =====================================================
-- schema_versions
-- =====================================================

CREATE TABLE IF NOT EXISTS schema_versions (
    version     BIGINT PRIMARY KEY,
    filename    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ DEFAULT NOW()
);

-- Baseline version stamp. This cumulative schema is the SQLite schema after all
-- migrations through 039 (memory_relationships_unique_edge), translated to PG —
-- so a PG deployment starts AT version 39, not by replaying the SQLite-dialect
-- NNN_*.up.sql files (which use rowid/AUTOINCREMENT/etc. that don't run on PG).
-- ON CONFLICT DO NOTHING keeps re-applying this file idempotent. Future PG-native
-- migrations continue the sequence from 40 as `pg_NNN_<name>.up.sql` (+ optional
-- `.down.sql`) files in THIS directory, applied by `bin/migrate_pg.py` (the PG
-- analogue of migrate_memory.py) — `PostgresBackend.ensure_schema` runs them
-- automatically after this baseline. Use PG dialect (no AUTOINCREMENT/FTS5/rowid),
-- one implicit transaction per file, no explicit COMMIT/BEGIN.
INSERT INTO schema_versions (version, filename)
VALUES (39, 'pg_primary_v1.sql')
ON CONFLICT (version) DO NOTHING;

COMMIT;
