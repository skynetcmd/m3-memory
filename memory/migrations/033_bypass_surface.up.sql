-- 033_bypass_surface — materialized rank-independent recall surface (ADR-0001).
--
-- Pure relevance ranking (top-k) misses answer-bearing memory items that exist in
-- the store but rank below the cut — enumeration/aggregation/confirmation queries
-- especially. Bypass-k surfaces those items by TYPED ATOM, rank-independently, gated
-- on the production strategy. Computing that per query is O(Q) work + a full scan per
-- run (wrong shape at scale), so this table MATERIALIZES the surfacing once (offline /
-- incrementally) and the retrieval path reads it with a single scope-isolated seek.
--
-- Derived data: rebuildable from entities + observations via
-- bin/memory/entity.build_bypass_surface(). Down-path is a clean DROP (ADR-0001 §10 Q5,
-- mirroring 032_entity_embeddings).
--
-- SCOPE (ADR-0001 §7): conversation_id is mandatory; user_id + scope mirror the
-- memory_items scoping so the read path inherits the caller's exact scope predicate and
-- cannot cross tenants. memory_id references the surfaced item (core's unit is
-- memory_items.id — there is no separate "turns" table; that is bench-only).
--
-- GDPR (ADR-0001 §7/§9): the FK ON DELETE CASCADE is defense-in-depth, but gdpr_forget
-- in bin/memory_maintenance.py purges by EXPLICIT enumeration and does not rely on
-- cascade — so bypass_surface is ALSO added to that enumeration. Both, by design.
CREATE TABLE IF NOT EXISTS bypass_surface (
    conversation_id TEXT NOT NULL,
    memory_id       TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    source          TEXT NOT NULL,                 -- 'entity' | 'observation'
    strategy        TEXT,                           -- routed strategy this was built under
    user_id         TEXT,
    scope           TEXT NOT NULL DEFAULT 'agent',
    cap             INTEGER,
    built_at        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (conversation_id, memory_id)
);

-- Hot-path read: WHERE conversation_id = ? AND scope = ? [AND user_id = ?].
-- Leading column conversation_id makes the per-scope seek a covering range (ADR §8).
CREATE INDEX IF NOT EXISTS idx_bypass_surface_scope
    ON bypass_surface(conversation_id, scope, user_id);
