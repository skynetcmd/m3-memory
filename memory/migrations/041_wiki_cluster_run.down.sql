-- 041_wiki_cluster_run.down.sql
-- Reverse 041: drop the wiki compile provenance + membership cache.
--
-- cluster_members is a pure cache (rebuildable from a recompute). cluster_run is
-- provenance, so dropping it discards compile-time history — acceptable on a
-- deliberate down-migration, since down-migrations are a schema rollback, not a
-- data-preservation path.

DROP INDEX IF EXISTS idx_cluster_members_hash;
DROP INDEX IF EXISTS idx_cluster_members_run;
DROP INDEX IF EXISTS idx_cluster_members_memory;
DROP TABLE IF EXISTS cluster_members;

DROP INDEX IF EXISTS idx_cluster_run_completed;
DROP INDEX IF EXISTS idx_cluster_run_seq;
DROP TABLE IF EXISTS cluster_run;
