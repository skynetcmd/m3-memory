-- 025_observation_queue.down.sql
--
-- Reverses v025. Safe because:
--  - Phase D Observer/Reflector is an optional, gated-off feature
--    (M3_PREFER_OBSERVATIONS=0 by default; bench harness opts in via
--    --observer-variant flag).
--  - Queue rows are ephemeral.
--  - type='observation' memory_items rows are NOT deleted; they remain
--    in memory_items as orphan rows. Callers should clean those up
--    manually if a full rollback is needed (DELETE FROM memory_items
--    WHERE type='observation';).

DROP INDEX IF EXISTS idx_mi_type_user_obs;
DROP INDEX IF EXISTS idx_rq_attempts;
DROP INDEX IF EXISTS idx_rq_user_conv;
DROP TABLE IF EXISTS reflector_queue;
DROP INDEX IF EXISTS idx_oq_attempts;
DROP INDEX IF EXISTS idx_oq_conversation_id;
DROP TABLE IF EXISTS observation_queue;
