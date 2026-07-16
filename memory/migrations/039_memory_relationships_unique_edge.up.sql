-- 039_memory_relationships_unique_edge.up.sql
-- Adds the UNIQUE (from_id, to_id, relationship_type) index that memory_link_impl
-- has always ASSUMED but never had.
--
-- Follows 038.
--
-- Why: memory_link_impl (bin/memory/write.py) writes edges with
--   INSERT OR REPLACE INTO memory_relationships (from_id, to_id, relationship_type)
-- intending idempotency — augment_memory.py explicitly documents "link-adjacent
-- uses memory_link_impl [and] memory_link_impl is responsible" for not creating
-- duplicates. But OR REPLACE only dedups against a UNIQUE/PK conflict, and the
-- only unique key on this table is the `id` PK — which is NOT in the insert. So
-- the OR REPLACE has nothing to conflict on and every call INSERTs a new row:
-- repeated links silently create duplicate edges. It has not bitten production
-- yet only because current callers happen not to re-link identical pairs.
--
-- This migration makes the intended semantics real: one edge per
-- (from_id, to_id, relationship_type). It ALSO unblocks the PostgreSQL backend —
-- without a real arbiter, that write could not be routed to ON CONFLICT and was
-- deferred; with this index it becomes a clean ON CONFLICT (from_id, to_id,
-- relationship_type) DO NOTHING on both backends.
--
-- Safety: a UNIQUE index creation FAILS if duplicates already exist, so we
-- de-duplicate FIRST (keep the lowest rowid per group), then create the index.
-- The dedup is a no-op on a clean DB (production currently has 0 duplicate
-- groups across ~10.5k edges). All-or-nothing under migrate_memory.py's
-- savepoint wrapper.

-- 1. Remove pre-existing duplicate edges, keeping the earliest row per group.
DELETE FROM memory_relationships
WHERE rowid NOT IN (
    SELECT MIN(rowid)
    FROM memory_relationships
    GROUP BY from_id, to_id, relationship_type
);

-- 2. Enforce the intended uniqueness going forward.
CREATE UNIQUE INDEX IF NOT EXISTS idx_mr_unique_edge
    ON memory_relationships(from_id, to_id, relationship_type);
