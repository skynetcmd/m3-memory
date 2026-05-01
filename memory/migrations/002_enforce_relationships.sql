-- 002_enforce_relationships.sql
-- Recreates memory_relationships to add ON DELETE CASCADE so orphaned relationships are cleaned up automatically.
-- Enables PRAGMA foreign_keys = ON safety in the DB.

-- Removed top-level BEGIN/COMMIT pair: migrate_memory.py wraps each
-- migration in its own SAVEPOINT, so a literal BEGIN here is a syntax
-- error inside that wrapper. SQLite's auto-commit mode plus the
-- migration runner's implicit transaction cover this migration's
-- atomicity needs.
PRAGMA foreign_keys = OFF;

CREATE TABLE IF NOT EXISTS memory_relationships_new (
    id                TEXT PRIMARY KEY,
    from_id           TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    to_id             TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,
    created_at        TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Copy existing data over, dropping any orphaned rows (where from_id or to_id doesn't exist anymore).
INSERT INTO memory_relationships_new (id, from_id, to_id, relationship_type, created_at)
SELECT mr.id, mr.from_id, mr.to_id, mr.relationship_type, mr.created_at
FROM memory_relationships mr
INNER JOIN memory_items mi1 ON mr.from_id = mi1.id
INNER JOIN memory_items mi2 ON mr.to_id = mi2.id;

DROP TABLE memory_relationships;
ALTER TABLE memory_relationships_new RENAME TO memory_relationships;

CREATE INDEX idx_mr_from ON memory_relationships(from_id);
CREATE INDEX idx_mr_to   ON memory_relationships(to_id);

PRAGMA foreign_keys = ON;
