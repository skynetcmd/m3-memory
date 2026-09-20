-- pg_058_memory_dynamics.up.sql
--
-- PostgreSQL counterpart of 049_memory_dynamics.up.sql. Adds the state a
-- decay/reinforcement cycle needs in order to be observable and reversible.
--
-- WHY (short form -- full rationale in the SQLite 049 header): m3 already had
-- working decay and working access tracking, and nothing connected them. A
-- memory retrieved 762 times decayed at exactly the same rate as one nobody had
-- touched in a year, and the decay_rate column declared back in 001 had no
-- reader and no writer -- average 0.0 across every row.
--
--   importance_raw                   the undecayed original. Decay overwrites
--                                    `importance` IN PLACE, so without this the
--                                    pre-decay value is gone: no forensic read,
--                                    no GDPR export of the original, no way to
--                                    explain a ranking.
--   helpful_count / unhelpful_count  graded feedback as TWO counters, never a
--                                    net. +5/-5 and +0/-0 net identically while
--                                    meaning opposite things, and asymmetric
--                                    weights cannot be applied to a pre-summed
--                                    number. Mirrors corroboration_count /
--                                    contradiction_count.
--
-- ⚠ importance_raw IS BACKFILLED TO `importance`, AND THAT START IS LOSSY. For
-- rows already decayed the true original is unrecoverable; they begin equal and
-- diverge from the next pass. Said plainly rather than implying recovery.
--
-- ⚠ THE REPAIR: `importance` is documented 0.0-1.0 but nothing clamped on write,
-- so rows exist holding 5.0-8.0. Every floor and multiplier in the new dynamics
-- assumes a bounded range. Repaired here once; enforced in the write path from
-- this release onward.

ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS importance_raw DOUBLE PRECISION DEFAULT NULL;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS helpful_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS unhelpful_count INTEGER NOT NULL DEFAULT 0;

-- Repair BEFORE seeding, so the raw baseline is inside the documented range.
UPDATE memory_items SET importance = 1.0 WHERE importance > 1.0;
UPDATE memory_items SET importance = 0.0 WHERE importance < 0.0;

UPDATE memory_items SET importance_raw = importance WHERE importance_raw IS NULL;

-- Partial index matching the only shape reinforcement reads.
CREATE INDEX IF NOT EXISTS idx_memory_items_reinforce
    ON memory_items(last_accessed_at, access_count)
    WHERE is_deleted = 0;

-- ⚠ THE CHATLOG CLONE — POSTGRES ONLY, AND THE REASON THIS FILE IS NOT JUST THE
-- SQLITE MIGRATION TRANSLATED.
--
-- On SQLite the chatlog is a SEPARATE FILE carrying the same table names, so
-- migration 049 reaches it as `memory_items`. On PG both stores share one
-- database and the chatlog is a CLONE, `chat_log_items` (pg_043), so a column
-- added to the core table alone silently misses the chatlog half — and any code
-- routed through dialect.chatlog_table() then hits a missing column on PG only.
--
-- This is exactly the class pg_049 was written to repair: pg_041 added `status`
-- to entity_extraction_queue, the clone never got it, and chatlog entity
-- extraction could not mark a turn terminal on PG, so the cognitive loop never
-- idled. test_schema_parity_pg_live.py::test_chatlog_clones_mirror_core_columns
-- guards the class; it caught this migration.
--
-- The clone gets the columns but NOT the reinforce index: nothing reinforces
-- chatlog turns (chatlog_decay.py owns their lifecycle), so the index would be
-- write cost with no reader.

ALTER TABLE chat_log_items ADD COLUMN IF NOT EXISTS importance_raw DOUBLE PRECISION DEFAULT NULL;
ALTER TABLE chat_log_items ADD COLUMN IF NOT EXISTS helpful_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE chat_log_items ADD COLUMN IF NOT EXISTS unhelpful_count INTEGER NOT NULL DEFAULT 0;

UPDATE chat_log_items SET importance = 1.0 WHERE importance > 1.0;
UPDATE chat_log_items SET importance = 0.0 WHERE importance < 0.0;

UPDATE chat_log_items SET importance_raw = importance WHERE importance_raw IS NULL;
