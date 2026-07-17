-- pg_043_chatlog_tables.up.sql
--
-- Chatlog clone of the 8 core primary tables (one-schema / two-table format).
--
-- m3-memory's chatlog store shares the SAME Postgres schema (default `public`
-- search_path) as the core memory store. It is NOT a separate schema/namespace:
-- the chatlog tables are distinguished from the core tables purely by NAME —
-- every chatlog table carries a `chat_log_` prefix (`chat_log_items`,
-- `chat_log_embeddings`, ...). Core `memory_items` and chatlog `chat_log_items`
-- live side by side in the same schema.
--
-- This migration mechanically clones these 8 core tables (from pg_primary_v1.sql)
-- and their indexes into their chat_log_* counterparts:
--
--   memory_items            -> chat_log_items
--   memory_embeddings       -> chat_log_embeddings
--   memory_relationships    -> chat_log_relationships
--   entities                -> chat_log_entities
--   memory_item_entities    -> chat_log_item_entities
--   entity_relationships    -> chat_log_entity_relationships
--   entity_extraction_queue -> chat_log_extraction_queue
--
-- Transformations applied:
--   * Table names renamed per the map above (NOT a blind string prefix).
--   * Column definitions copied verbatim (same columns/types/defaults),
--     including chat_log_items' GENERATED search_vector tsvector column.
--   * FOREIGN KEY references rewritten to point at the chat_log_* counterpart —
--     a chatlog table NEVER FKs to a core table.
--   * Index names given a chatlog-distinct prefix (idx_cl_...) so they do not
--     collide with the core indexes in the shared schema. Columns / predicates /
--     WHERE clauses are otherwise unchanged.
--
-- NOT cloned: schema_versions (chatlog shares core's), memory_items_fts
-- (no PG analogue — search_vector covers keyword search), and all other
-- core-only tables (agents, notifications, tasks, memory_history, etc.).
--
-- Idempotent: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS /
-- ADD COLUMN IF NOT EXISTS throughout. No explicit BEGIN/COMMIT — migrate_pg.py
-- wraps each file in a single transaction.

-- =====================================================
-- chat_log_items  (clone of memory_items)
-- =====================================================

CREATE TABLE IF NOT EXISTS chat_log_items (
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

CREATE INDEX IF NOT EXISTS idx_cl_items_type       ON chat_log_items(type);
CREATE INDEX IF NOT EXISTS idx_cl_items_agent      ON chat_log_items(agent_id);
CREATE INDEX IF NOT EXISTS idx_cl_items_model      ON chat_log_items(model_id);
CREATE INDEX IF NOT EXISTS idx_cl_items_created    ON chat_log_items(created_at);
CREATE INDEX IF NOT EXISTS idx_cl_items_deleted    ON chat_log_items(is_deleted);
CREATE INDEX IF NOT EXISTS idx_cl_items_deleted_type ON chat_log_items(is_deleted, type);
CREATE INDEX IF NOT EXISTS idx_cl_items_importance   ON chat_log_items(importance);
CREATE INDEX IF NOT EXISTS idx_cl_items_updated      ON chat_log_items(updated_at);
CREATE INDEX IF NOT EXISTS idx_cl_items_change_agent ON chat_log_items(change_agent);
CREATE INDEX IF NOT EXISTS idx_cl_items_user_id ON chat_log_items(user_id);
CREATE INDEX IF NOT EXISTS idx_cl_items_scope ON chat_log_items(scope);
CREATE INDEX IF NOT EXISTS idx_cl_items_valid_from ON chat_log_items(valid_from);
CREATE INDEX IF NOT EXISTS idx_cl_items_handoff_inbox
    ON chat_log_items(agent_id, type, read_at, created_at);
CREATE INDEX IF NOT EXISTS idx_cl_items_refresh_on
    ON chat_log_items(refresh_on)
    WHERE refresh_on IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cl_items_conversation_id
    ON chat_log_items(conversation_id, created_at)
    WHERE is_deleted = 0;

-- Full-text search: GENERATED search_vector tsvector column (title weighted 'A',
-- content 'B') + GIN index, mirroring memory_items. Chatlog keyword search needs
-- it (no FTS5 analogue on PG).
ALTER TABLE chat_log_items
    ADD COLUMN IF NOT EXISTS search_vector tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(content, '')), 'B')
    ) STORED;
CREATE INDEX IF NOT EXISTS idx_cl_items_search_vector
    ON chat_log_items USING GIN (search_vector);
CREATE INDEX IF NOT EXISTS idx_cl_items_active_type_created
    ON chat_log_items(is_deleted, type, created_at DESC)
    WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_cl_items_scope_type
    ON chat_log_items(scope, type)
    WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_cl_items_user_agent
    ON chat_log_items(user_id, agent_id);
CREATE INDEX IF NOT EXISTS idx_cl_items_content_hash
    ON chat_log_items(content_hash)
    WHERE content_hash != '';
CREATE INDEX IF NOT EXISTS idx_cl_items_source ON chat_log_items(source);
CREATE INDEX IF NOT EXISTS idx_cl_items_variant
    ON chat_log_items(variant) WHERE variant IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cl_items_user_variant
    ON chat_log_items(user_id, variant) WHERE variant IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cl_items_chat_log
    ON chat_log_items (conversation_id, created_at)
    WHERE type='chat_log' AND is_deleted=0;

CREATE INDEX IF NOT EXISTS idx_cl_items_host_agent
    ON chat_log_items ((metadata_json->>'host_agent'))
    WHERE type='chat_log';
CREATE INDEX IF NOT EXISTS idx_cl_items_provider
    ON chat_log_items ((metadata_json->>'provider'))
    WHERE type='chat_log';
CREATE INDEX IF NOT EXISTS idx_cl_items_metadata_model_id
    ON chat_log_items ((metadata_json->>'model_id'))
    WHERE type='chat_log';
CREATE INDEX IF NOT EXISTS idx_cl_items_provider_time
    ON chat_log_items ((metadata_json->>'provider'), created_at)
    WHERE type='chat_log' AND is_deleted=0;
CREATE INDEX IF NOT EXISTS idx_cl_items_type_user_obs
  ON chat_log_items(type, user_id, valid_from)
  WHERE type='observation';
CREATE INDEX IF NOT EXISTS idx_cl_items_id_prefix8
    ON chat_log_items(SUBSTR(id, 1, 8));
CREATE INDEX IF NOT EXISTS idx_cl_items_source_group
    ON chat_log_items(source_group_id) WHERE source_group_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cl_items_stage1_kg_pending
    ON chat_log_items(variant, type)
    WHERE stage1_kg_done = 0;
CREATE INDEX IF NOT EXISTS idx_cl_items_confidence
    ON chat_log_items(confidence) WHERE confidence IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_cl_items_pinned
    ON chat_log_items(pinned) WHERE pinned = 1;

-- =====================================================
-- chat_log_embeddings  (clone of memory_embeddings)
-- =====================================================

CREATE TABLE IF NOT EXISTS chat_log_embeddings (
    id          TEXT PRIMARY KEY,
    memory_id   TEXT NOT NULL REFERENCES chat_log_items(id) ON DELETE CASCADE,
    embedding   BYTEA NOT NULL,
    embed_model TEXT DEFAULT 'jina-embeddings-v5',
    dim         BIGINT DEFAULT 1024,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    content_hash TEXT,
    vector_kind TEXT NOT NULL DEFAULT 'default'
);

CREATE INDEX IF NOT EXISTS idx_cl_embeddings_memory_id  ON chat_log_embeddings(memory_id);
CREATE INDEX IF NOT EXISTS idx_cl_embeddings_memory_model
    ON chat_log_embeddings(memory_id, embed_model);
CREATE INDEX IF NOT EXISTS idx_cl_embeddings_content_hash_model
    ON chat_log_embeddings(content_hash, embed_model);
CREATE INDEX IF NOT EXISTS idx_cl_embeddings_memory_kind
    ON chat_log_embeddings(memory_id, vector_kind);

-- =====================================================
-- chat_log_relationships  (clone of memory_relationships)
-- =====================================================

CREATE TABLE IF NOT EXISTS chat_log_relationships (
    id                TEXT PRIMARY KEY,
    from_id           TEXT NOT NULL REFERENCES chat_log_items(id) ON DELETE CASCADE,
    to_id             TEXT NOT NULL REFERENCES chat_log_items(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,
    created_at        TIMESTAMPTZ DEFAULT NOW(),
    weight            DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_cl_relationships_from ON chat_log_relationships(from_id);
CREATE INDEX IF NOT EXISTS idx_cl_relationships_to   ON chat_log_relationships(to_id);
CREATE INDEX IF NOT EXISTS idx_cl_relationships_rel_type ON chat_log_relationships(relationship_type);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cl_relationships_unique_edge
    ON chat_log_relationships(from_id, to_id, relationship_type);

-- =====================================================
-- chat_log_entities  (clone of entities)
-- =====================================================

CREATE TABLE IF NOT EXISTS chat_log_entities (
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
CREATE INDEX IF NOT EXISTS idx_cl_entities_canonical_type ON chat_log_entities(canonical_name, entity_type);
CREATE INDEX IF NOT EXISTS idx_cl_entities_type           ON chat_log_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_cl_entities_hash           ON chat_log_entities(content_hash);

-- =====================================================
-- chat_log_item_entities  (clone of memory_item_entities)
-- =====================================================

CREATE TABLE IF NOT EXISTS chat_log_item_entities (
    memory_id       TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    mention_text    TEXT,
    mention_offset  INTEGER DEFAULT 0,
    confidence      DOUBLE PRECISION DEFAULT 0.85,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (memory_id, entity_id, mention_offset),
    FOREIGN KEY (memory_id) REFERENCES chat_log_items(id)    ON DELETE CASCADE,
    FOREIGN KEY (entity_id) REFERENCES chat_log_entities(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_cl_item_entities_entity ON chat_log_item_entities(entity_id);

-- =====================================================
-- chat_log_entity_relationships  (clone of entity_relationships)
-- =====================================================

CREATE TABLE IF NOT EXISTS chat_log_entity_relationships (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_entity      TEXT NOT NULL,
    to_entity        TEXT NOT NULL,
    predicate        TEXT NOT NULL,
    confidence       DOUBLE PRECISION DEFAULT 0.85,
    valid_from       TIMESTAMPTZ,
    valid_to         TIMESTAMPTZ,
    source_memory_id TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW(),
    FOREIGN KEY (from_entity)      REFERENCES chat_log_entities(id) ON DELETE CASCADE,
    FOREIGN KEY (to_entity)        REFERENCES chat_log_entities(id) ON DELETE CASCADE,
    FOREIGN KEY (source_memory_id) REFERENCES chat_log_items(id)    ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_cl_entity_rel_from      ON chat_log_entity_relationships(from_entity, predicate);
CREATE INDEX IF NOT EXISTS idx_cl_entity_rel_to        ON chat_log_entity_relationships(to_entity, predicate);
CREATE INDEX IF NOT EXISTS idx_cl_entity_rel_predicate ON chat_log_entity_relationships(predicate);

-- =====================================================
-- chat_log_extraction_queue  (clone of entity_extraction_queue)
-- =====================================================

CREATE TABLE IF NOT EXISTS chat_log_extraction_queue (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    memory_id       TEXT NOT NULL,
    enqueued_at     TIMESTAMPTZ DEFAULT NOW(),
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT,
    last_attempt_at TIMESTAMPTZ,
    FOREIGN KEY (memory_id) REFERENCES chat_log_items(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cl_extraction_queue_memory_id ON chat_log_extraction_queue(memory_id);
CREATE INDEX IF NOT EXISTS idx_cl_extraction_queue_attempts ON chat_log_extraction_queue(attempts, enqueued_at);
