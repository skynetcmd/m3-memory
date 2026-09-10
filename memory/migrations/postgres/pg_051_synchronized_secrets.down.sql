-- pg_051_synchronized_secrets.down.sql — revert pg_051.
--
-- ⚠ DESTRUCTIVE: this table holds the ONLY copy of any secret stored in the
-- vault tier on this deployment (env and keyring live elsewhere). Dropping it
-- discards those secrets, and on a PG primary they are not recoverable from a
-- file the way a SQLite vault would be. Down-migrate only if you have confirmed
-- the rows are reproducible from another source -- e.g. still present on the
-- warehouse, or re-settable via `m3 secrets set`.
DROP INDEX IF EXISTS idx_ss_updated;
DROP TABLE IF EXISTS synchronized_secrets;
