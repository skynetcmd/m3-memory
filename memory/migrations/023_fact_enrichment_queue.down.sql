-- 023_fact_enrichment_queue.down.sql
--
-- Reverses v023. Safe because:
--  - Fact enrichment is an optional, gated-off feature (M3_ENABLE_FACT_ENRICHED=false by default).
--  - Queue rows are ephemeral; deleting them just means pending work is lost
--    but can be re-enqueued on the next write pass.
--  - The fact_enriched memory items themselves (created by the enricher) are NOT
--    deleted by this migration — they remain in memory_items but are orphaned
--    (their source reference edges point to deleted queue rows, which is harmless).
--    Callers should manually clean up fact_enriched rows if needed.

DROP INDEX IF EXISTS idx_feq_attempts;
DROP INDEX IF EXISTS idx_feq_memory_id;
DROP TABLE IF EXISTS fact_enrichment_queue;
