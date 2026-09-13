-- 006_embedding_vector_kind.up.sql
--
-- Ports main-lane 022 into the CHATLOG lane. Same column, same default,
-- same index shape -- because the two lanes are read and written by the
-- SAME embedding writer.
--
-- WHY THIS EXISTS. agent_chatlog.db carries its own `memory_embeddings`
-- table (001_bootstrap) and its own migration series, but the writer is
-- SHARED with the main store. When main gained `vector_kind` at 022 and
-- the chatlog lane did not, every embedding write routed at the chatlog
-- DB began raising:
--
--     OperationalError: table memory_embeddings has no column named vector_kind
--
-- Measured consequence on a macOS host, 2026-09-12: 55 chatlog turns
-- (2026-07-04 and 2026-08-09) landed in the spill quarantine instead of
-- the store, and sat there unnoticed until someone went looking. They
-- were recoverable, but nothing reported the loss.
--
-- THE STRUCTURAL POINT, not just this column. Two migration series and
-- one shared writer means a schema change to a shared table can reach
-- one lane and not the other, and nothing catches it until a write fails
-- at runtime. That is DESIGN_PHILOSOPHIES 10a's duplication concern in
-- schema form: "Duplicated predicate/SQL logic is the defect,
-- independent of correctness. Copies drift." This migration closes the
-- instance; tests/test_chatlog_lane_schema_parity.py guards the class.
--
-- Default 'default' matches 022 exactly, so existing chatlog rows keep
-- resolving through (memory_id, embed_model, vector_kind='default') for
-- any caller written before either lane had the column.

-- 1. Add the column with the back-compat default for existing rows.
ALTER TABLE memory_embeddings ADD COLUMN vector_kind TEXT NOT NULL DEFAULT 'default';

-- 2. Composite index for the per-kind lookup pattern (mirrors 022).
CREATE INDEX IF NOT EXISTS idx_me_memory_kind
    ON memory_embeddings(memory_id, vector_kind);

-- 3. Refresh stats after a schema change on a heavily-indexed table.
ANALYZE memory_embeddings;
