-- pg_058_memory_dynamics.down.sql
-- Reverses pg_058. Counterpart of 049_memory_dynamics.down.sql.
--
-- ⚠ ROLLING BACK IS LOSSY IN ONE DIRECTION THAT MATTERS.
--
-- Dropping helpful_count / unhelpful_count discards accumulated feedback --
-- regrettable, but agents can grade again.
--
-- Dropping importance_raw is different: it is the ONLY record of the undecayed
-- value, and `importance` has been decayed in place since pg_058 ran. After
-- rollback there is no way to recover a memory's pre-decay importance. Capture
-- (id, importance, importance_raw) first if that baseline matters.
--
-- The out-of-range importance repair is NOT reversed: those values violated the
-- documented 0.0-1.0 contract and corrupted every floor/ceiling computation.
-- Restoring them would restore the bug.

DROP INDEX IF EXISTS idx_memory_items_reinforce;
ALTER TABLE memory_items DROP COLUMN IF EXISTS unhelpful_count;
ALTER TABLE memory_items DROP COLUMN IF EXISTS helpful_count;
ALTER TABLE memory_items DROP COLUMN IF EXISTS importance_raw;
