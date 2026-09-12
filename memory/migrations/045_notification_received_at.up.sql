-- 045_notification_received_at.up.sql
--
-- Separates TRANSPORT RECEIPT from AGENT CONSUMPTION on notifications.
--
-- WHY: `notifications` carried ONE timestamp, `read_at`. The agent-to-agent
-- delivery SLA is "acknowledge receipt within 30 seconds", and a subprocess
-- waiter (bin/m3_notification_waiter.py) can satisfy it -- notifications_ack_all
-- round-trips in ~430 ms, far inside the budget. But acking on DETECTION wrote
-- `read_at`, marking a message read that no agent had read, and that flag was
-- the only record the work was still pending. So the waiter had to ship with
-- --ack OFF by default: the SLA was reachable but not safely reachable.
--
-- The obvious counter-argument -- "the task state machine covers it, an unacked
-- task is still open" -- was measured rather than assumed, and is false for real
-- traffic: 29 of 30 recent notifications carried no task_id at all. Nothing else
-- recorded that the message was outstanding.
--
-- So: two columns for two different events.
--   received_at -- set by the transport when the message lands. Satisfies the
--                  <30s receipt SLA. Says nothing about whether an agent has
--                  looked at it.
--   read_at     -- set by an agent actually consuming the message. Unchanged in
--                  meaning. --unread_only still filters on it, so the unread
--                  flag keeps working exactly as before.
--
-- Deliberately NOT backfilled. A pre-existing row's receipt time is genuinely
-- unknown, and writing created_at into it would be a guess that later reads
-- could not distinguish from a real measurement (DESIGN_PHILOSOPHIES 12c --
-- prefer the cheap measurement over the plausible model). NULL means "we do not
-- know when this was received", which is true.
--
-- The partial index mirrors idx_mi_refresh_on's shape: only rows where the
-- column is set are interesting, so the index stays small on a table whose old
-- rows all hold NULL here.

ALTER TABLE notifications ADD COLUMN received_at TEXT;

CREATE INDEX IF NOT EXISTS idx_notif_received_at
    ON notifications(agent_id, received_at)
    WHERE received_at IS NOT NULL;
