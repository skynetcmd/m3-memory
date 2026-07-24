-- pg_045_wiki_cluster_run.down.sql
-- Reverse pg_045: drop the wiki compile provenance + membership cache on PG.
-- Mirrors 041_wiki_cluster_run.down.sql.

DROP INDEX IF EXISTS idx_cluster_members_hash;
DROP INDEX IF EXISTS idx_cluster_members_run;
DROP INDEX IF EXISTS idx_cluster_members_memory;
DROP TABLE IF EXISTS cluster_members;

DROP INDEX IF EXISTS idx_cluster_run_completed;
DROP INDEX IF EXISTS idx_cluster_run_seq;
DROP TABLE IF EXISTS cluster_run;
