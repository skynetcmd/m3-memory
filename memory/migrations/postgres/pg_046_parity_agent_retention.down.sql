-- pg_046_parity_agent_retention.down.sql
--
-- Reverse of pg_046_parity_agent_retention.up.sql: drop the agent_retention_policies
-- table added for parity with SQLite.
-- No explicit BEGIN/COMMIT — migrate_pg.py wraps the file in one transaction.

DROP TABLE IF EXISTS agent_retention_policies;
