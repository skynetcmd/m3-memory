-- 008_performance_tuning.sql
-- Add indexes for improved search performance and system tuning.

-- Optimize agent_filter in memory_search_impl
CREATE INDEX IF NOT EXISTS idx_mi_change_agent ON memory_items(change_agent);

-- Optimize watermark lookups in pg_sync
CREATE INDEX IF NOT EXISTS idx_mi_updated ON memory_items(updated_at);

-- Performance tuning
ANALYZE;
