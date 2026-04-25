-- 024_entity_graph.down.sql
--
-- Reverses v024. Safe because:
--  - Entity graph is an optional, gated-off feature (M3_ENABLE_ENTITY_GRAPH=false by default).
--  - Queue rows are ephemeral; deleting them just means pending work is lost
--    but can be re-enqueued on the next write pass.
--  - The entity rows themselves are not tied to any other persistent state;
--    dropping them is safe as long as all downstream code is prepared for
--    a schema downgrade (which it is, since the feature is off by default).

DROP INDEX IF EXISTS idx_eeq_attempts;
DROP INDEX IF EXISTS idx_eeq_memory_id;
DROP TABLE IF EXISTS entity_extraction_queue;

DROP INDEX IF EXISTS idx_er_predicate;
DROP INDEX IF EXISTS idx_er_to;
DROP INDEX IF EXISTS idx_er_from;
DROP TABLE IF EXISTS entity_relationships;

DROP INDEX IF EXISTS idx_mie_entity;
DROP TABLE IF EXISTS memory_item_entities;

DROP INDEX IF EXISTS idx_entities_hash;
DROP INDEX IF EXISTS idx_entities_type;
DROP INDEX IF EXISTS idx_entities_canonical_type;
DROP TABLE IF EXISTS entities;
