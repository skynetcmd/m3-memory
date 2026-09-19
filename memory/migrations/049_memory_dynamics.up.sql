-- 049_memory_dynamics.up.sql
--
-- Give m3's memory the second half of a consolidation model: the columns a
-- decay/reinforcement cycle needs in order to be observable and reversible.
--
-- ── WHY ──────────────────────────────────────────────────────────────────────
--
-- Everything needed for human-like memory dynamics already existed EXCEPT the
-- connections between the pieces. Measured on 4,034 live rows, 2026-09-19:
--
--   decay            WORKS. importance * 0.995 for rows >7d, pinned-exempt
--                    (memory_maintenance.py). Old-unpinned avg importance 0.296
--                    vs recent 0.695 vs pinned 0.852 -- the gradient is real.
--   access tracking  WORKS. last_accessed_at / access_count, batched flush
--                    every 250ms (memory/db.py). 1,034 rows carry counts; the
--                    most-read memory has been accessed 762 times.
--   reinforcement    MISSING. Nothing reads access_count to strengthen anything.
--   decay_rate       INERT. Declared in 001 with DEFAULT 0.0; no reader and no
--                    writer outside pg-sync plumbing. Average across every row:
--                    exactly 0.0.
--
-- So a memory retrieved 762 times decayed at precisely the same rate as one
-- nobody had touched in a year. This migration adds the state that fixes that.
--
-- ── THE COLUMNS ──────────────────────────────────────────────────────────────
--
-- importance_raw   The undecayed original. Decay overwrites `importance` IN
--                  PLACE, so without this the pre-decay value is unrecoverable
--                  -- which breaks forensic reads ("what did I believe on
--                  2026-03-01"), GDPR export, and any explanation of why a row
--                  ranked where it did. It is written on insert and NEVER
--                  decayed.
--
--                  ⚠ BACKFILLED TO `importance`, AND THAT IS A LOSSY START.
--                  For rows already decayed the true original is gone; this
--                  cannot reconstruct it. Those rows begin equal and diverge
--                  from the next pass onward. Stated plainly rather than
--                  pretending to recover history (048 sets the precedent for
--                  saying what a backfill does and does not mean).
--
-- helpful_count    Graded post-answer feedback, kept as TWO counters and never
-- unhelpful_count  as a net. A net is lossy exactly where it matters: +5/-5
--                  (contested, heavily used) and +0/-0 (never seen) both net to
--                  zero while meaning opposite things. Separate counters also
--                  allow asymmetric weighting, which a pre-summed number cannot
--                  express. This mirrors corroboration_count /
--                  contradiction_count, which the confidence model already
--                  keeps apart for the same reason.
--
-- ── THE REPAIR ───────────────────────────────────────────────────────────────
--
-- `importance` is documented as 0.0-1.0 everywhere, but the memory_write
-- ToolSpec declares no minimum/maximum and nothing clamped on write, so three
-- rows hold 5.0-8.0. Every floor and multiplier in the new dynamics assumes a
-- bounded range: MAX(floor, importance * 0.995) on an 8.0 row decays for ~1,400
-- days before re-entering the intended band, outranking every legitimate memory
-- the whole time. The clamp is enforced in code from this release; this is the
-- one-shot repair of the rows written before it existed.

ALTER TABLE memory_items ADD COLUMN importance_raw REAL DEFAULT NULL;
ALTER TABLE memory_items ADD COLUMN helpful_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE memory_items ADD COLUMN unhelpful_count INTEGER NOT NULL DEFAULT 0;

-- Repair out-of-range importance BEFORE seeding importance_raw, so the raw
-- baseline is inside the documented range rather than preserving the bug.
UPDATE memory_items SET importance = 1.0 WHERE importance > 1.0;
UPDATE memory_items SET importance = 0.0 WHERE importance < 0.0;

-- Seed the undecayed baseline. See the lossy-start warning above.
UPDATE memory_items SET importance_raw = importance WHERE importance_raw IS NULL;

-- Reinforcement reads "recently accessed, not pinned" on every maintenance
-- pass; that is the only shape it is queried in.
CREATE INDEX IF NOT EXISTS idx_memory_items_reinforce
    ON memory_items(last_accessed_at, access_count)
    WHERE is_deleted = 0;
