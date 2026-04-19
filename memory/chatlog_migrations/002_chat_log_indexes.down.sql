-- 002_chat_log_indexes.down.sql
DROP INDEX IF EXISTS idx_memory_items_chat_log;
DROP INDEX IF EXISTS idx_memory_items_host_agent;
DROP INDEX IF EXISTS idx_memory_items_provider;
DROP INDEX IF EXISTS idx_memory_items_model_id;
DROP INDEX IF EXISTS idx_memory_items_provider_time;
