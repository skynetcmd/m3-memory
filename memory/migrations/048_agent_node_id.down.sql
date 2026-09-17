-- 048_agent_node_id.down.sql
-- Reverses 048. Requires SQLite >= 3.35 for DROP COLUMN (same floor as
-- 014/045/046/047).
--
-- Rolling back loses the node attribution and nothing else: no delivery state
-- lives here, and every agent re-stamps node_id on its next heartbeat if the
-- column returns. A fleet that rolls back simply falls back to the documented
-- ambiguous case, which fails toward PostgreSQL.

DROP INDEX IF EXISTS idx_agents_node_last_seen;
ALTER TABLE agents DROP COLUMN node_id;
