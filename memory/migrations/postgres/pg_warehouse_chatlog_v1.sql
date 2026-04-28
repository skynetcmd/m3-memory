-- m3-memory warehouse schema migration: chatlog tables
-- Run on your PostgreSQL warehouse (e.g., 10.21.40.51:5432).
-- Mirrors SQLite tables from agent_chatlog.db so chat logs sync across peers.
-- agent_memory.db schema is NOT in this file (already exists in warehouse).
--
-- Apply via psql:
--   psql -h <warehouse-host> -U <user> -d m3_memory -f pg_warehouse_chatlog_v1.sql
--
-- Idempotent: uses CREATE TABLE IF NOT EXISTS throughout. Safe to re-run.

BEGIN;

CREATE SCHEMA IF NOT EXISTS m3_warehouse;
SET search_path TO m3_warehouse, public;

-- =====================================================
-- agent_chatlog.db tables
-- =====================================================

-- Note: SQLite FTS5 virtual tables (memory_items_fts, memory_items_fts_config,
-- memory_items_fts_data, memory_items_fts_docsize, memory_items_fts_idx) are NOT mirrored.
-- Postgres will use native tsvector for full-text search; see warehouse search indexing separately.

-- Chat log turns: user role, assistant role, provider, model
CREATE TABLE IF NOT EXISTS m3_warehouse.memory_items (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  title TEXT,
  content TEXT,
  metadata_json JSONB,
  agent_id TEXT,
  model_id TEXT,
  change_agent TEXT DEFAULT 'unknown',
  importance DOUBLE PRECISION DEFAULT 0.5,
  source TEXT DEFAULT 'agent',
  origin_device TEXT DEFAULT 'macbook',
  is_deleted INTEGER DEFAULT 0,
  expires_at TIMESTAMPTZ,
  decay_rate DOUBLE PRECISION DEFAULT 0.0,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  last_accessed_at TIMESTAMPTZ,
  access_count BIGINT DEFAULT 0,
  user_id TEXT,
  scope TEXT,
  valid_from TIMESTAMPTZ,
  valid_to TIMESTAMPTZ,
  content_hash TEXT,
  read_at TIMESTAMPTZ,
  conversation_id TEXT,
  refresh_on TIMESTAMPTZ,
  refresh_reason TEXT,
  variant TEXT
);

CREATE INDEX IF NOT EXISTS idx_memory_items_updated_at ON m3_warehouse.memory_items(updated_at);
CREATE INDEX IF NOT EXISTS idx_mi_type ON m3_warehouse.memory_items(type);
CREATE INDEX IF NOT EXISTS idx_mi_agent ON m3_warehouse.memory_items(agent_id);
CREATE INDEX IF NOT EXISTS idx_mi_model ON m3_warehouse.memory_items(model_id);
CREATE INDEX IF NOT EXISTS idx_mi_created ON m3_warehouse.memory_items(created_at);
CREATE INDEX IF NOT EXISTS idx_mi_deleted ON m3_warehouse.memory_items(is_deleted);
CREATE INDEX IF NOT EXISTS idx_mi_conversation_composite ON m3_warehouse.memory_items(conversation_id, created_at) WHERE is_deleted = 0;
CREATE INDEX IF NOT EXISTS idx_memory_items_chat_log ON m3_warehouse.memory_items(conversation_id, created_at) WHERE type='chat_log' AND is_deleted=0;

-- Chat log metadata extraction via JSON: host_agent, provider, model_id
-- Note: these are expression indexes on metadata_json; may require special Postgres setup
-- CREATE INDEX IF NOT EXISTS idx_memory_items_host_agent ON m3_warehouse.memory_items((metadata_json->>'host_agent')) WHERE type='chat_log';
-- CREATE INDEX IF NOT EXISTS idx_memory_items_provider ON m3_warehouse.memory_items((metadata_json->>'provider')) WHERE type='chat_log' AND is_deleted=0;
-- CREATE INDEX IF NOT EXISTS idx_memory_items_provider_time ON m3_warehouse.memory_items((metadata_json->>'provider'), created_at) WHERE type='chat_log' AND is_deleted=0;
-- CREATE INDEX IF NOT EXISTS idx_memory_items_model_id ON m3_warehouse.memory_items((metadata_json->>'model_id')) WHERE type='chat_log';

-- Vector embeddings for semantic search (blob storage)
CREATE TABLE IF NOT EXISTS m3_warehouse.memory_embeddings (
  id TEXT PRIMARY KEY,
  memory_id TEXT NOT NULL REFERENCES m3_warehouse.memory_items(id) ON DELETE CASCADE,
  embedding BYTEA NOT NULL,
  embed_model TEXT DEFAULT 'jina-embeddings-v5',
  dim BIGINT DEFAULT 1024,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  content_hash TEXT,
  vector_kind TEXT NOT NULL DEFAULT 'default',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  is_deleted INTEGER NOT NULL DEFAULT 0,
  deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_memory_embeddings_updated_at ON m3_warehouse.memory_embeddings(updated_at);
CREATE INDEX IF NOT EXISTS idx_me_memory_id ON m3_warehouse.memory_embeddings(memory_id);
CREATE INDEX IF NOT EXISTS idx_me_content_hash_model ON m3_warehouse.memory_embeddings(content_hash, embed_model);
CREATE INDEX IF NOT EXISTS idx_me_memory_kind ON m3_warehouse.memory_embeddings(memory_id, vector_kind);

-- Memory relationship graph: typed edges (related, supports, contradicts, extends, supersedes, references, consolidates, message, handoff)
CREATE TABLE IF NOT EXISTS m3_warehouse.memory_relationships (
  id TEXT PRIMARY KEY,
  from_id TEXT NOT NULL REFERENCES m3_warehouse.memory_items(id),
  to_id TEXT NOT NULL REFERENCES m3_warehouse.memory_items(id),
  relationship_type TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  is_deleted INTEGER NOT NULL DEFAULT 0,
  deleted_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_memory_relationships_updated_at ON m3_warehouse.memory_relationships(updated_at);
CREATE INDEX IF NOT EXISTS idx_mr_from ON m3_warehouse.memory_relationships(from_id);
CREATE INDEX IF NOT EXISTS idx_mr_to ON m3_warehouse.memory_relationships(to_id);

-- Metadata tracking: schema versions, sync watermarks, encrypted secrets
CREATE TABLE IF NOT EXISTS m3_warehouse.schema_versions (
  version BIGINT PRIMARY KEY,
  filename TEXT NOT NULL,
  applied_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS m3_warehouse.sync_watermarks (
  direction TEXT PRIMARY KEY,
  last_synced_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS m3_warehouse.synchronized_secrets (
  service_name TEXT PRIMARY KEY,
  encrypted_value TEXT NOT NULL,
  version BIGINT DEFAULT 1,
  origin_device TEXT,
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- Statistics table (SQLite auto-generated; Postgres uses ANALYZE)
CREATE TABLE IF NOT EXISTS m3_warehouse.sqlite_stat1 (
  tbl TEXT,
  idx TEXT,
  stat TEXT
);

COMMIT;
