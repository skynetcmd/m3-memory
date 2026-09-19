-- 049_memory_dynamics.down.sql
-- Reverses 049. Requires SQLite >= 3.35 for DROP COLUMN (same floor as
-- 014/045/046/047/048).
--
-- ⚠ ROLLING BACK IS LOSSY IN ONE DIRECTION THAT MATTERS.
--
-- Dropping helpful_count / unhelpful_count discards accumulated feedback, which
-- is merely regrettable -- agents can grade again.
--
-- Dropping importance_raw is different: it is the ONLY record of the undecayed
-- value, and `importance` has been decayed in place since this migration ran.
-- After rollback there is no way to recover what a memory's importance was
-- before decay touched it. If that baseline matters, capture
-- (id, importance, importance_raw) before rolling back.
--
-- The out-of-range importance repair (5.0-8.0 -> 1.0) is NOT reversed. Those
-- values violated the documented 0.0-1.0 contract and were corrupting every
-- floor/ceiling computation; restoring them would restore the bug. The clamp in
-- the write path stays regardless of this migration's state.

DROP INDEX IF EXISTS idx_memory_items_reinforce;
ALTER TABLE memory_items DROP COLUMN unhelpful_count;
ALTER TABLE memory_items DROP COLUMN helpful_count;
ALTER TABLE memory_items DROP COLUMN importance_raw;
