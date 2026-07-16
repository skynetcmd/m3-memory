-- pg_043_chatlog_tables.down.sql
--
-- Reverse of pg_043_chatlog_tables.up.sql: drop the 8 chat_log_* clone tables.
--
-- Dropped in reverse-dependency order — tables that hold FKs first, then the
-- FK targets (chat_log_items and chat_log_entities are referenced by others)
-- last. CASCADE also handles the FK edges, but ordering keeps intent explicit.
-- No explicit BEGIN/COMMIT — migrate_pg.py wraps each file in one transaction.

DROP TABLE IF EXISTS chat_log_chroma_sync_queue CASCADE;
DROP TABLE IF EXISTS chat_log_extraction_queue CASCADE;
DROP TABLE IF EXISTS chat_log_entity_relationships CASCADE;
DROP TABLE IF EXISTS chat_log_item_entities CASCADE;
DROP TABLE IF EXISTS chat_log_relationships CASCADE;
DROP TABLE IF EXISTS chat_log_embeddings CASCADE;
DROP TABLE IF EXISTS chat_log_entities CASCADE;
DROP TABLE IF EXISTS chat_log_items CASCADE;
