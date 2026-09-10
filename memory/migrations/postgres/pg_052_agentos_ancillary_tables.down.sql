-- pg_052_agentos_ancillary_tables.down.sql — revert pg_052.
--
-- ⚠ DESTRUCTIVE: drops audit/decision history (activity_logs, project_decisions)
-- along with the two smaller tables. These are append-only records on most
-- deployments, so a down-migration discards history that is not reproducible
-- from any other source. Confirm you have a dump before running it.
DROP TABLE IF EXISTS system_focus;
DROP TABLE IF EXISTS hardware_specs;
DROP TABLE IF EXISTS project_decisions;
DROP TABLE IF EXISTS activity_logs;
