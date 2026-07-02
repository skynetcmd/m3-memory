-- 038_entity_extraction_status.up.sql
-- Adds `status` to entity_extraction_queue so a PROCESSED-but-empty entity
-- extraction is terminal instead of being re-selected forever.
--
-- Follows 037. (033 is the last public entity/queue-adjacent migration; 034 was
-- consumed on a benchmark branch, so the public sequence runs ...036, 037, 038.)
--
-- Why: the extraction selection treated "already extracted" as "has >=1 row in
-- memory_item_entities". A turn that legitimately extracts to ZERO entities left
-- no such row and no failure row, so it was re-selected and re-sent to the LLM on
-- every run (worst-first under ORDER BY LENGTH DESC), burning tokens. This column
-- lets a processed turn be marked done (entities emitted OR empty) so selection
-- can skip it; failed rows keep status='failed'/NULL and stay retry-eligible.
--
-- Pure additive: existing queue rows default to NULL status (treated as
-- retry-eligible, i.e. today's behavior). A runtime ADD COLUMN fallback
-- (m3_entities.py::_ensure_extraction_status_column) also creates it so a DB
-- touched before migrating degrades gracefully — this migration is the
-- authoritative source of the column.
--
--   status  TEXT  — 'done' = processed (entities or empty); 'failed' = last
--                   attempt errored; NULL = never processed (retry-eligible).

ALTER TABLE entity_extraction_queue ADD COLUMN status TEXT;

-- Partial index: the selection anti-join skips done rows. Only 'done' rows pay
-- index cost; retry-eligible (NULL/'failed') rows are not indexed.
CREATE INDEX IF NOT EXISTS idx_eeq_status_done
    ON entity_extraction_queue(memory_id) WHERE status = 'done';
