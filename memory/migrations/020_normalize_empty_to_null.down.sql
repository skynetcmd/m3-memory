-- 020_normalize_empty_to_null.down.sql
--
-- Rollback. There is no safe reversal: we cannot distinguish rows that
-- were originally NULL from rows that this migration normalized from "".
-- We could blanket-rewrite every NULL back to "" but that would corrupt
-- rows that were legitimately NULL before the migration ran (e.g. the
-- 1384 pre-existing variant=NULL rows on my live DB).
--
-- Keep this as a no-op so schema_versions records the rollback cleanly
-- without touching data.

SELECT 1;
