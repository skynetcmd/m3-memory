-- 006_fts_and_features.sql
-- Full-Text Search (FTS5), retrieval tracking, scratchpad support.
-- All statements are additive — safe to re-run.

-- ── Full-Text Search virtual table ──────────────────────────────────────────
-- External-content FTS5 table backed by memory_items (zero-copy storage).

CREATE VIRTUAL TABLE IF NOT EXISTS memory_items_fts USING fts5(
    title, content, content=memory_items, content_rowid=rowid
);

-- Sync triggers: keep FTS index in lockstep with memory_items
CREATE TRIGGER IF NOT EXISTS mi_fts_insert AFTER INSERT ON memory_items BEGIN
    INSERT INTO memory_items_fts(rowid, title, content)
    VALUES (new.rowid, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS mi_fts_delete AFTER DELETE ON memory_items BEGIN
    INSERT INTO memory_items_fts(memory_items_fts, rowid, title, content)
    VALUES ('delete', old.rowid, old.title, old.content);
END;

CREATE TRIGGER IF NOT EXISTS mi_fts_update AFTER UPDATE ON memory_items BEGIN
    INSERT INTO memory_items_fts(memory_items_fts, rowid, title, content)
    VALUES ('delete', old.rowid, old.title, old.content);
    INSERT INTO memory_items_fts(rowid, title, content)
    VALUES (new.rowid, new.title, new.content);
END;

-- ── Retrieval tracking columns ──────────────────────────────────────────────
-- Enables feedback loop and frequency-based boosting.

ALTER TABLE memory_items ADD COLUMN last_accessed_at TEXT;
ALTER TABLE memory_items ADD COLUMN access_count INTEGER DEFAULT 0;

-- ── Backfill FTS index with existing data ───────────────────────────────────
INSERT INTO memory_items_fts(rowid, title, content)
    SELECT rowid, title, content FROM memory_items;
