-- pg_052_agentos_ancillary_tables.up.sql
--
-- Parity fix (continuation of pg_051): four AgentOS ancillary tables existed on
-- SQLite and were never mirrored into the PostgreSQL primary schema. Surfaced by
-- test_every_sqlite_core_table_exists_on_pg, the completeness gate added with
-- pg_051 -- it enumerates the SQLite schema instead of a hand-maintained
-- allowlist, so it found these the first time it ran.
--
-- Each fails on a PG primary the same way the secrets vault did: the query raises
-- UndefinedTable and whatever swallows it reports "empty" rather than "broken".
-- Readers, verified by grep rather than assumed:
--   activity_logs      bin/audit_trail.py, bin/mission_control.py
--   project_decisions  bin/audit_trail.py, bin/custom_tool_bridge.py
--   system_focus       bin/ai_mechanic.py, bin/custom_tool_bridge.py
--   hardware_specs     no current readers -- ported anyway, because a table that
--                      exists on one backend and not the other is drift whether
--                      or not today's code happens to read it.
--
-- NOT ported, deliberately: `sync_watermarks`. bin/pg_sync.py documents it as
-- "SQLite side, per direction+target", only ever touches it through `sl_cur`,
-- and creates it itself at :146-155. It tracks how far THIS SQLite store has
-- synced to a remote; a copy on the PG side would be a second, divergent
-- bookkeeping row for the same relationship. Backend-specific by design, so it
-- stays in _SQLITE_ONLY_TABLES rather than being mirrored.
--
-- Type mapping follows the rest of this schema: INTEGER PRIMARY KEY AUTOINCREMENT
-- -> BIGSERIAL, TEXT/DATETIME timestamps -> TIMESTAMPTZ DEFAULT NOW(). The
-- SQLite `thinking_stream` VIEW over activity_logs is NOT recreated: it is built
-- from strftime() and is a SQLite-only convenience with no reader in shipped code.
--
-- CREATE TABLE IF NOT EXISTS is idempotent; migrate_pg wraps the file in one
-- implicit transaction.

CREATE TABLE IF NOT EXISTS activity_logs (
    timestamp   TIMESTAMPTZ DEFAULT NOW(),
    query       TEXT,
    response    TEXT,
    model_used  TEXT DEFAULT 'DeepSeek-R1-70B'
);

CREATE TABLE IF NOT EXISTS project_decisions (
    id         BIGSERIAL PRIMARY KEY,
    project    TEXT,
    decision   TEXT,
    rationale  TEXT,
    timestamp  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS hardware_specs (
    id         BIGSERIAL PRIMARY KEY,
    component  TEXT,
    spec       TEXT,
    timestamp  TIMESTAMPTZ DEFAULT NOW()
);

-- system_focus uses a plain INTEGER PRIMARY KEY on SQLite (no AUTOINCREMENT):
-- callers write an explicit id (it is a single-row "current focus" ticker), so
-- BIGINT rather than BIGSERIAL preserves that write pattern.
CREATE TABLE IF NOT EXISTS system_focus (
    id         BIGINT PRIMARY KEY,
    summary    TEXT,
    timestamp  TIMESTAMPTZ DEFAULT NOW()
);
