-- pg_056_notification_lease.up.sql
--
-- PostgreSQL counterpart of 047_notification_lease.up.sql. Same intent, native
-- types: TIMESTAMPTZ rather than SQLite's TEXT, matching the other timestamp
-- columns in pg_primary_v1.sql.
--
-- WHY (short form -- full rationale in the SQLite 047 header): 046 gave a claim
-- an owner but no expiry, so a row held by an agent that died stayed claimed
-- forever. A static timeout is the wrong fix (it redelivers work a live worker
-- is still doing); a RENEWABLE lease is not. No `status` column: all four
-- states derive from columns already present, and a stored status would shadow
-- the six live `read_at IS NULL` predicates rather than replace them.
--
-- claim_expires_at is always written from the DATABASE clock (NOW()), never a
-- caller's -- m3 spans two hosts via pg_sync, so an N-clock expiry comparison
-- is a correctness bug. lease_token fences complete, fail AND renew.

ALTER TABLE notifications ADD COLUMN IF NOT EXISTS claim_expires_at TIMESTAMPTZ;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS lease_token      TEXT;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS attempt_count    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS failed_at        TIMESTAMPTZ;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS conversation_id  TEXT;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS reply_to_id      BIGINT;

CREATE INDEX IF NOT EXISTS idx_notif_expired_lease
    ON notifications(claim_expires_at)
    WHERE claim_expires_at IS NOT NULL AND read_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_notif_conversation
    ON notifications(conversation_id, id)
    WHERE conversation_id IS NOT NULL;
