-- 016_performance_indexes.up.sql
-- Additional indexes for common query patterns and data integrity checks.
--
-- idx_mi_active_type_created: covers the hot path
--   SELECT ... FROM memory_items WHERE is_deleted = 0 AND type = ? ORDER BY created_at DESC
--   The existing idx_mi_deleted_type lacks created_at, forcing a filesort.
--
-- idx_mi_scope_type: covers scope-filtered queries (e.g. "all user-scope devices")
--
-- idx_mi_user_agent: fast benchmark-data filtering (user_id != '' AND agent_id = '')
--
-- idx_mi_content_hash: integrity verification and dedup lookups by SHA-256
--
-- idx_me_memory_model: covers the embedding JOIN in hybrid search
--   (memory_id alone is indexed, but search also filters by embed_model)

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

CREATE INDEX IF NOT EXISTS idx_me_memory_model
    ON memory_embeddings(memory_id, embed_model);

ANALYZE;
