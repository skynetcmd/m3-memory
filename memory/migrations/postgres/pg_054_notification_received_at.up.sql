-- pg_054_notification_received_at.up.sql
--
-- PostgreSQL counterpart of 045_notification_received_at.up.sql. Same intent,
-- native types: TIMESTAMPTZ rather than SQLite's TEXT, matching read_at's
-- declaration in pg_primary_v1.sql.
--
-- WHY (short form -- the full rationale is in the SQLite 045 header) the
-- notifications table carried ONE timestamp, read_at, so a transport waiter
-- acking to satisfy the <30s receipt SLA destroyed the unread flag that was the
-- only record the work was still pending. "The task state machine covers it"
-- was measured and is false: 29 of 30 recent notifications carried no task_id.
--
--   received_at -- transport receipt. Satisfies the SLA.
--   read_at     -- agent consumption. Unchanged -- --unread_only still uses it.
--
-- Deliberately NOT backfilled: a pre-existing row's receipt time is unknown, and
-- writing created_at into it would be a guess indistinguishable from a real
-- measurement. NULL means "unknown", which is true.
--
-- IF NOT EXISTS on the ADD COLUMN because the warehouse may be converged from a
-- newer pg_primary_v1.sql that already declares the column, so re-running must not
-- fail. SQLite has no such form, which is why 045 states it plainly instead.

ALTER TABLE notifications ADD COLUMN IF NOT EXISTS received_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_notif_received_at
    ON notifications(agent_id, received_at)
    WHERE received_at IS NOT NULL;
