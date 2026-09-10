-- pg_051_synchronized_secrets.up.sql
--
-- Parity fix: `synchronized_secrets` -- the encrypted secrets vault -- existed on
-- SQLite (003_encrypted_secrets.sql, 007_synchronized_secrets.sql) and in the
-- WAREHOUSE schema (pg_warehouse_chatlog_v1.sql, as
-- m3_warehouse.synchronized_secrets), but NOT in the PostgreSQL PRIMARY schema.
-- pg_primary_v1.sql had zero references and no numbered pg_NNN migration added it.
--
-- WHY IT MATTERED (measured on m3_test at schema_version 48, not inferred):
-- auth_utils.get_api_key's cascade is env -> keyring -> native store -> encrypted
-- vault. That last tier queries `synchronized_secrets` unconditionally, with no
-- os.path.exists gate, precisely so it works on PG where the vault has no FILE.
-- With the table absent the query raised UndefinedTable, get_api_key swallowed it
-- (`except Exception` around the vault read), and EVERY vault-stored secret
-- silently resolved to None on a PostgreSQL primary. Not an error the operator
-- ever saw -- just a credential that was "not configured", on a deployment where
-- `m3 secrets set` had visibly succeeded on SQLite.
--
-- Same class as pg_049 and pg_050: a table/column that SQLite gets from the
-- shared migration chain, which the PG schema has to be told about explicitly.
-- Surfaced by tests/test_serve_auth_pg_live.py while proving the serve-auth
-- vault probe on a live cluster.
--
-- Types mirror m3_warehouse.synchronized_secrets exactly (verified against the
-- live warehouse: text/text/bigint/text/timestamptz), so pg_sync's bidirectional
-- copy between a primary and the warehouse moves rows between IDENTICAL shapes.
-- INTEGER -> BIGINT and TEXT-timestamp -> TIMESTAMPTZ are the standard mappings
-- used elsewhere in this schema; auth_utils writes an ISO-8601 UTC string, which
-- PostgreSQL casts into TIMESTAMPTZ on insert (the warehouse has done exactly
-- this in production for every synced row).
--
-- CREATE TABLE / CREATE INDEX IF NOT EXISTS are idempotent; migrate_pg wraps the
-- file in one implicit transaction.

CREATE TABLE IF NOT EXISTS synchronized_secrets (
    service_name    TEXT PRIMARY KEY,
    encrypted_value TEXT NOT NULL,
    version         BIGINT DEFAULT 1,
    origin_device   TEXT,
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ss_updated ON synchronized_secrets(updated_at);
