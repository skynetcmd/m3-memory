-- 009_scoping_and_history.sql
-- Memory scoping (user/session/agent/org) and change audit trail.
-- All statements are additive — safe to re-run.

-- ── Memory Scoping ─────────────────────────────────────────────────────────────
-- Enables multi-user isolation and session-scoped auto-expiry.

ALTER TABLE memory_items ADD COLUMN user_id TEXT DEFAULT '';
ALTER TABLE memory_items ADD COLUMN scope TEXT DEFAULT 'agent';

CREATE INDEX IF NOT EXISTS idx_mi_user_id ON memory_items(user_id);
CREATE INDEX IF NOT EXISTS idx_mi_scope ON memory_items(scope);

-- ── Memory History / Audit Trail ───────────────────────────────────────────────
-- Tracks every create/update/delete/supersede event per memory item.
-- Inspired by Mem0's SQLiteManager.add_history and Graphiti's bi-temporal model.

CREATE TABLE IF NOT EXISTS memory_history (
    id          TEXT PRIMARY KEY,
    memory_id   TEXT NOT NULL,
    event       TEXT NOT NULL,
    prev_value  TEXT,
    new_value   TEXT,
    field       TEXT DEFAULT 'content',
    actor_id    TEXT DEFAULT '',
    created_at  TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_mh_memory_id ON memory_history(memory_id);
CREATE INDEX IF NOT EXISTS idx_mh_created ON memory_history(created_at);
