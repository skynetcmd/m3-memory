-- 004_entity_graph_align.down.sql
--
-- Remove the entity-relation graph tables from the chatlog DB.

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
