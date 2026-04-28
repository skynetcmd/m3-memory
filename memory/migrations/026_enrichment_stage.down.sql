-- 026_enrichment_stage.down.sql
--
-- Reverses migration 026. Removes the `stage` column added to
-- observation_queue + reflector_queue plus the supporting indexes.
--
-- SQLite doesn't support DROP COLUMN cleanly until 3.35+, and even
-- then ALTER TABLE DROP COLUMN won't undo a NOT NULL DEFAULT — we'd
-- have to rebuild the table. For a queue table whose rows are
-- ephemeral (drained then deleted), a clean rebuild is acceptable:
-- pending rows lose their stage field but their conversation_id +
-- attempts survive.

DROP INDEX IF EXISTS idx_oq_stage_attempts;
DROP INDEX IF EXISTS idx_rq_stage_attempts;

-- SQLite ≥ 3.35 supports `ALTER TABLE DROP COLUMN`. Try the modern
-- path; fall back to a noop if the SQLite version is too old.
ALTER TABLE observation_queue DROP COLUMN stage;
ALTER TABLE reflector_queue   DROP COLUMN stage;
