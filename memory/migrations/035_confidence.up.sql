-- 035_confidence.up.sql
-- Adds first-class confidence/trust columns to memory_items for the
-- knowledge-maintenance work (see docs/plans/KNOWLEDGE_MAINTENANCE_PLAN.md).
--
-- Numbered 035 (not 034) to avoid a migration-numbering collision: some
-- deployments' databases already carry a 034 from a divergent migration line,
-- so 035 sits above both to keep the public sequence monotonic and conflict-free.
--
-- All columns default NULL / 0 so this is a pure additive, non-rewriting
-- migration: existing rows are untouched and a NULL `confidence` is interpreted
-- everywhere as "fall back to importance" — flag-off behavior is unchanged.
-- (Same add-column-with-neutral-default precedent as 006/009/010/018.)
--
--   confidence          REAL  — transparent, user-facing aggregate in [0,1].
--                              NULL = not yet derived (treat as importance).
--   belief_alpha        REAL  — optional Beta posterior alpha (ranking experiments).
--   belief_beta         REAL  — optional Beta posterior beta.
--   corroboration_count INT   — distinct corroborating sources recorded so far.
--   contradiction_count INT   — contradictions recorded against this memory.

ALTER TABLE memory_items ADD COLUMN confidence REAL DEFAULT NULL;
ALTER TABLE memory_items ADD COLUMN belief_alpha REAL DEFAULT NULL;
ALTER TABLE memory_items ADD COLUMN belief_beta REAL DEFAULT NULL;
ALTER TABLE memory_items ADD COLUMN corroboration_count INTEGER DEFAULT 0;
ALTER TABLE memory_items ADD COLUMN contradiction_count INTEGER DEFAULT 0;

-- Partial index: only rows that have a derived confidence pay index cost.
-- Supports the flag-gated confidence-ranking path without scanning NULLs.
CREATE INDEX IF NOT EXISTS idx_memory_items_confidence
    ON memory_items(confidence) WHERE confidence IS NOT NULL;
