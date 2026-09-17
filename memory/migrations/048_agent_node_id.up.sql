-- 048_agent_node_id.up.sql
--
-- Record WHICH MACHINE each agent runs on, so the fleet's topology is an
-- observable fact rather than a guess.
--
-- ── WHY ──────────────────────────────────────────────────────────────────────
--
-- Notification delivery has two viable backends and they are not
-- interchangeable:
--
--   SQLite  single-node.  Arrival detection watches the WAL file's mtime+size,
--                         a FILESYSTEM signal. Lighter and faster, and a WAL on
--                         machine A cannot wake a waiter on machine B.
--   Postgres multi-node.  Detection polls a shared table, so it crosses hosts.
--
-- Choosing wrongly toward SQLite on a real multi-node fleet means agents on
-- other hosts NEVER RECEIVE MAIL -- silently, with no error anywhere, because
-- the mechanism cannot report a boundary it cannot see. Choosing wrongly toward
-- Postgres costs some efficiency on a single node. Those are not comparable, so
-- the decision fails toward Postgres when the evidence is ambiguous.
--
-- Making it evidence-based needs one fact the registry did not record: the node.
-- `agent_id` carries a SESSION instance ("claude-code@a1b2c3") but never a host,
-- so two agents could not be told apart by machine.
--
-- ── WHY ON THE HEARTBEAT ─────────────────────────────────────────────────────
--
-- `last_seen` is already written on every poll, and a registry row is forever
-- while a heartbeat is recent. Detection must count only agents that are
-- ACTUALLY ACTIVE: measured 2026-09-12, a waiter ran with `antigravity-agent`
-- (last seen in MAY) while the live agent went unwatched -- 36 notifications,
-- zero receipts, for hours. Writing node_id alongside last_seen means the node
-- fact ages out exactly as liveness does, at no extra cost.
--
-- ⚠ NOT BACKFILLED, deliberately, following 047 ("Nothing is backfilled...
-- NULL and 0 say exactly that"). NULL here means "this agent has not checked in
-- since the column existed", which is precisely what a detector must treat as
-- unknown rather than as evidence of a single node. Backfilling it with the
-- local host would manufacture the exact false certainty that sends the
-- decision down the silent-failure branch.

ALTER TABLE agents ADD COLUMN node_id TEXT DEFAULT NULL;

-- The detection query: distinct nodes among RECENTLY ACTIVE agents. Indexed on
-- (node_id, last_seen) because that is the only shape it is read in, and it
-- runs on a decision path rather than a background sweep.
CREATE INDEX IF NOT EXISTS idx_agents_node_last_seen
    ON agents(node_id, last_seen)
    WHERE node_id IS NOT NULL;
