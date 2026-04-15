-- 016_performance_indexes.down.sql
DROP INDEX IF EXISTS idx_mi_active_type_created;
DROP INDEX IF EXISTS idx_mi_scope_type;
DROP INDEX IF EXISTS idx_mi_user_agent;
DROP INDEX IF EXISTS idx_mi_content_hash;
DROP INDEX IF EXISTS idx_me_memory_model;
