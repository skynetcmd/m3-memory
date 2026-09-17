-- pg_057_agent_node_id.up.sql
--
-- PostgreSQL counterpart of 048_agent_node_id.up.sql. Record WHICH MACHINE each
-- agent runs on, so the fleet's topology is an observable fact rather than a
-- guess.
--
-- WHY (short form -- full rationale in the SQLite 048 header): notification
-- delivery has two viable backends and they are not interchangeable. SQLite
-- detection watches a WAL file's mtime+size, a FILESYSTEM signal that cannot
-- cross a machine boundary; PostgreSQL polls a shared table, which can. Getting
-- that choice wrong toward SQLite on a real multi-node fleet means agents on
-- other hosts never receive mail, silently. Getting it wrong toward PostgreSQL
-- costs some efficiency. The decision therefore fails toward PostgreSQL when
-- the evidence is ambiguous -- and this column is the evidence.
--
-- Written on the HEARTBEAT, beside last_seen, so the node fact ages out exactly
-- as liveness does: a registry row is forever, a heartbeat is recent, and
-- detection must count only agents that are actually active.
--
-- ⚠ NOT BACKFILLED, deliberately. NULL means "has not checked in since the
-- column existed", which a detector must treat as UNKNOWN rather than as
-- evidence of a single node. Backfilling with the local host would manufacture
-- the false certainty that sends the decision down the silent-failure branch.

ALTER TABLE agents ADD COLUMN IF NOT EXISTS node_id TEXT DEFAULT NULL;

-- The detection query: distinct nodes among RECENTLY ACTIVE agents. Partial for
-- the same reason as the SQLite index -- a row with no node is never a
-- detection candidate and would be dead weight.
CREATE INDEX IF NOT EXISTS idx_agents_node_last_seen
    ON agents(node_id, last_seen)
    WHERE node_id IS NOT NULL;
