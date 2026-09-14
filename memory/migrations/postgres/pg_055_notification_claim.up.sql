-- pg_055_notification_claim.up.sql
--
-- PostgreSQL counterpart of 046_notification_claim.up.sql. Same intent, native
-- types: TIMESTAMPTZ rather than SQLite's TEXT, matching read_at/received_at's
-- declaration in pg_primary_v1.sql.
--
-- WHY (short form -- full rationale in the SQLite 046 header): a bare TYPE
-- queue is a FAN-OUT read, so two sister sessions polling one role queue both
-- pick up the same item and do it twice. read_at records THAT a row was
-- consumed, not by WHICH instance, and received_at is transport receipt --
-- neither can arbitrate between sisters. claimed_by names the winner.
--
-- The index differs from SQLite's on purpose. PG claims with FOR UPDATE SKIP
-- LOCKED, which takes row locks on candidates it inspects, so the index wants
-- to narrow the candidate set to unclaimed rows for a given addressee before
-- any locking happens -- hence the same partial-index shape, which PG supports
-- natively.

ALTER TABLE notifications ADD COLUMN IF NOT EXISTS claimed_by TEXT;
ALTER TABLE notifications ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_notif_claimable
    ON notifications(agent_id, id)
    WHERE claimed_by IS NULL;
