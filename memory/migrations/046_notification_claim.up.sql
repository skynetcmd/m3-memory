-- 046_notification_claim.up.sql
--
-- Atomic work-stealing for sister agents: which INSTANCE owns a message.
--
-- WHY: a bare TYPE queue ("claude-code") is a FAN-OUT read -- every sister
-- session sees the same rows. That is correct for a broadcast and wrong for
-- work: two sisters polling one role queue both pick up the same item and do
-- it twice. Measured 2026-09-12 (see 012/#170): two sessions on one identity
-- saw the identical pair of items, and one session's ack made them vanish
-- from the other mid-flight.
--
-- `read_at` cannot express this. It records THAT the row was consumed, not by
-- WHICH instance, so it cannot arbitrate between sisters. `received_at` (045)
-- is transport receipt and is equally silent on ownership -- its own test file
-- says so: it records that *an* instance received the message, not which one.
--
--   claimed_by -- the qualified agent id that won the row ("claude-code@a1b2c3").
--                 NULL means unclaimed and available to any sister.
--   claimed_at -- when the claim was taken. Diagnostic today; the input a
--                 fencing/expiry scheme would need later. No sweeper reads it
--                 yet, deliberately: a static timeout reverting CLAIMED to
--                 PENDING races the agent still working, and a timeout that
--                 fires on a legitimately long task is a false alarm
--                 (DESIGN_PHILOSOPHIES 3 -- a warning that fires when nothing
--                 is wrong trains people to ignore the one that matters).
--
-- Deliberately NOT backfilled. An existing row was never claimed by anybody;
-- NULL says exactly that and is true.
--
-- The partial index mirrors 045's shape: only unclaimed rows are ever scanned
-- by a claim, so the index stays small on a table whose claimed rows are dead
-- weight to that query.

ALTER TABLE notifications ADD COLUMN claimed_by TEXT;
ALTER TABLE notifications ADD COLUMN claimed_at TEXT;

CREATE INDEX IF NOT EXISTS idx_notif_claimable
    ON notifications(agent_id, id)
    WHERE claimed_by IS NULL;
