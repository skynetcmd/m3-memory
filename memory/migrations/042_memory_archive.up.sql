-- 042_memory_archive.up.sql
-- Memory archive (tombstone sink for maintenance-evicted items).
--
-- Follows 041.
--
-- BACKGROUND / bug this fixes. The archive was previously a SEPARATE SQLite
-- sidecar file (agent_memory_archive.db, ARCHIVE_DB_PATH) whose `archived_items`
-- table was NEVER created by any migration or bootstrap. _transfer_to_archive
-- wrapped its INSERT in `except Exception: return False`, so every archive write
-- silently no-opped on `no such table` — archiving has been dead on SQLite the
-- whole time, and there was no PG path at all. This moves the archive INTO the
-- primary store as a normal migrated table routed through the seam, so it works
-- identically on SQLite and PostgreSQL, participates in GDPR erasure (user_id is
-- captured), and is covered by schema parity.
--
-- NATURE. Write-mostly tombstone: the maintenance pass (retention limits, expiry
-- purge, low-importance auto-archive) records WHAT left the live store and WHY,
-- before the live row is soft-deleted/deleted. Not a restore queue — nothing
-- reads it back today — but keeping type/title/agent_id/user_id makes it a useful
-- audit trail and, critically, lets a GDPR erasure reach archived rows by user_id.
--
-- id is the ORIGINAL memory_items.id (a re-archive of the same id overwrites via
-- upsert — the seam's on_conflict_update on the id PK — so this is idempotent).

CREATE TABLE IF NOT EXISTS memory_archive (
    id              TEXT PRIMARY KEY,   -- original memory_items.id
    type            TEXT,
    title           TEXT,
    content         TEXT,
    agent_id        TEXT,
    user_id         TEXT,               -- captured so GDPR erasure can reach archived rows
    archive_reason  TEXT NOT NULL,      -- 'expired' | 'retention_limit' | 'low_importance'
    archived_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_memory_archive_user   ON memory_archive(user_id);
CREATE INDEX IF NOT EXISTS idx_memory_archive_reason ON memory_archive(archive_reason);
CREATE INDEX IF NOT EXISTS idx_memory_archive_at     ON memory_archive(archived_at);
