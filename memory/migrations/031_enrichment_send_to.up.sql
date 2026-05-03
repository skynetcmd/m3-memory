-- 031_enrichment_send_to.up.sql
--
-- Adds a `send_to` column to enrichment_groups, recording which provider
-- is *assigned* to process each row. Distinct from any actual extractor
-- attribution: `send_to` is the routing instruction; the worker that
-- reads it should only claim rows whose send_to matches its own name.
--
-- Use case: parallel multi-provider runs (e.g. Grok + Gemini both
-- processing the same source variant, with disjoint subsets allocated
-- by content size or any other criterion). Each provider's worker
-- reads only rows where send_to matches its name; rows with send_to
-- IS NULL are reserved (not claimable by any --send-to-tagged worker)
-- so that an explicit assignment step is required for routed runs.
--
-- Backwards-compatible: when no --send-to flag is passed, existing
-- behavior is preserved (the worker ignores the column entirely and
-- claims any pending row).
--
-- Idempotent under bin/migrate_memory.py: re-applying triggers "duplicate
-- column name" which the runner treats as already-applied.

ALTER TABLE enrichment_groups ADD COLUMN send_to TEXT;
CREATE INDEX IF NOT EXISTS idx_eg_send_to ON enrichment_groups(send_to, status);
