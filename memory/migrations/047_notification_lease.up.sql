-- 047_notification_lease.up.sql
--
-- Renewable leases, fencing, retry limits, and conversation threading.
--
-- WHY A LEASE AND NOT A TIMEOUT: 046 gave a claim an owner but no expiry, so a
-- row claimed by an agent that then died stayed claimed forever. The obvious
-- fix -- a static timeout reverting CLAIMED to PENDING -- is the one the queue
-- literature warns about: SQS/Celery visibility timeouts redeliver a message
-- EVEN IF the worker is still processing it, which duplicates work, and sizing
-- the timeout to the longest legitimate task delays every real recovery. A
-- RENEWABLE lease removes the tradeoff: a live worker renews (at roughly
-- ttl/3, so it can absorb two consecutive renewal failures), and only a worker
-- that stopped renewing loses its claim.
--
-- WHY NOT A `status` COLUMN. All four states are already derivable from the
-- columns present, so storing a status string would give "is this pending" TWO
-- sources of truth that nothing forces to agree:
--   PENDING   = claimed_by IS NULL AND read_at IS NULL
--   CLAIMED   = claimed_by IS NOT NULL AND read_at IS NULL AND failed_at IS NULL
--   COMPLETED = read_at IS NOT NULL
--   FAILED    = failed_at IS NOT NULL
-- Six live call sites already filter on `read_at IS NULL`, one of them the
-- waiter that delivers agent mail. A status column would not replace those
-- predicates, it would SHADOW them, and the first write path that sets one
-- without the other breaks delivery silently. §10a -- a copied predicate is the
-- defect independent of correctness. `failed_at` is added because FAILED is the
-- one state genuinely not expressible today; the rest are derived by a single
-- predicate owner in the seam.
--
--   claim_expires_at -- when the lease lapses. ALWAYS written from the DATABASE
--                       clock, never a caller's: m3 spans two machines via
--                       pg_sync, and letting each agent judge expiry by its own
--                       clock makes two agents reach different verdicts about
--                       the same lease.
--   lease_token      -- opaque per-ATTEMPT fence. Required to complete, to fail,
--                       AND to renew. The third is the one that is easy to miss:
--                       a worker whose lease was reclaimed must not be able to
--                       renew its way back into ownership.
--   attempt_count    -- incremented per claim, so a message that keeps being
--                       reclaimed can be dead-lettered instead of retried
--                       forever.
--   conversation_id / reply_to_id -- threading, so a multi-step exchange can be
--                       reassembled. Additive and independent of the lease.
--
-- Nothing is backfilled. An existing row has no lease, no attempts and no
-- thread; NULL and 0 say exactly that.

ALTER TABLE notifications ADD COLUMN claim_expires_at TEXT;
ALTER TABLE notifications ADD COLUMN lease_token      TEXT;
ALTER TABLE notifications ADD COLUMN attempt_count    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE notifications ADD COLUMN failed_at        TEXT;
ALTER TABLE notifications ADD COLUMN conversation_id  TEXT;
ALTER TABLE notifications ADD COLUMN reply_to_id      INTEGER;

-- The sweeper's only query: expired leases, oldest first. Partial, because a
-- row without a lease is never a sweep candidate and would be dead weight.
CREATE INDEX IF NOT EXISTS idx_notif_expired_lease
    ON notifications(claim_expires_at)
    WHERE claim_expires_at IS NOT NULL AND read_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_notif_conversation
    ON notifications(conversation_id, id)
    WHERE conversation_id IS NOT NULL;
