-- pg_047_memory_archive.up.sql
--
-- PostgreSQL mirror of SQLite migration 042_memory_archive.up.sql — the memory
-- archive tombstone table, moved from the never-created SQLite sidecar file into
-- the primary store so archiving works on both backends through the seam. See
-- the SQLite migration header for the full background (the archive silently
-- no-opped for its entire prior existence).
--
-- Type mapping mirrors the SQLite intent: TEXT->TEXT, strftime() default -> NOW()
-- / TIMESTAMPTZ. CREATE TABLE / INDEX IF NOT EXISTS keep this idempotent;
-- migrate_pg wraps the file in one transaction.

CREATE TABLE IF NOT EXISTS memory_archive (
    id              TEXT PRIMARY KEY,   -- original memory_items.id
    type            TEXT,
    title           TEXT,
    content         TEXT,
    agent_id        TEXT,
    user_id         TEXT,               -- captured so GDPR erasure can reach archived rows
    archive_reason  TEXT NOT NULL,      -- 'expired' | 'retention_limit' | 'low_importance'
    archived_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memory_archive_user   ON memory_archive(user_id);
CREATE INDEX IF NOT EXISTS idx_memory_archive_reason ON memory_archive(archive_reason);
CREATE INDEX IF NOT EXISTS idx_memory_archive_at     ON memory_archive(archived_at);
