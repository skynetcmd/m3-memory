-- pg_040_observer_enrich_tables.down.sql — revert pg_040.
-- Drops the observer/reflector queues and enrichment run/group tables. Does NOT
-- touch memory_items.source_group_id (owned by the v39 baseline, not this file).
-- One implicit transaction (migrate_pg.py wraps it); no explicit BEGIN/COMMIT.

DROP TABLE IF EXISTS enrichment_groups CASCADE;
DROP TABLE IF EXISTS enrichment_runs CASCADE;
DROP TABLE IF EXISTS reflector_queue CASCADE;
DROP TABLE IF EXISTS observation_queue CASCADE;
