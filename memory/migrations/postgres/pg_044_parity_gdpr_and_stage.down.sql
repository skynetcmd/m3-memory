-- pg_044_parity_gdpr_and_stage.down.sql
--
-- Reverse of pg_044_parity_gdpr_and_stage.up.sql: drop the gdpr_requests table
-- and the two queue `stage` columns (with their indexes) added for parity.
-- No explicit BEGIN/COMMIT — migrate_pg.py wraps the file in one transaction.

DROP INDEX IF EXISTS idx_rq_stage_attempts;
DROP INDEX IF EXISTS idx_oq_stage_attempts;
ALTER TABLE reflector_queue   DROP COLUMN IF EXISTS stage;
ALTER TABLE observation_queue DROP COLUMN IF EXISTS stage;
DROP TABLE IF EXISTS gdpr_requests;
