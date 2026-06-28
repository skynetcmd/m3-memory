-- 036_trust_and_corroboration.up.sql
-- Phase 2 of the knowledge-maintenance plan: trust-weighted & consensus
-- provenance. See docs/plans/KNOWLEDGE_MAINTENANCE_PLAN.md.
--
-- Two additions, both additive and neutral by default:
--
-- 1. agents.trust_score — a per-agent reliability multiplier, default 1.0
--    (neutral, so existing agents are unaffected). Bounded [0.5,1.0] by the
--    application; used to weight that agent's assertions in confidence
--    aggregation and (opt-in) auto-tuned later. Set explicitly via the
--    agent_set_trust tool; M3_TRUST_AUTOTUNE stays off by default.
--
-- 2. memory_corroborations — an append-only ledger of corroboration and
--    contradiction events against a memory. Mirrors the memory_history
--    append-only pattern: replayable, auditable, and the source of the
--    aggregation inputs (distinct-trust-sum, counts) so confidence is never
--    re-derived by scanning the whole table on read.
--      memory_id   — the memory being corroborated/contradicted.
--      source_kind — 'agent' | 'user' | 'internet' | 'observer' | ...
--      source_ref  — the asserting agent_id / source identifier (free-form).
--      trust_at_write — the source's trust_score at the moment of the event
--                       (frozen so historical aggregation is reproducible).
--      delta       — +trust for corroboration, -trust for contradiction.
--      created_at  — event time.

ALTER TABLE agents ADD COLUMN trust_score REAL DEFAULT 1.0;

CREATE TABLE IF NOT EXISTS memory_corroborations (
    id             TEXT PRIMARY KEY,
    memory_id      TEXT NOT NULL,
    source_kind    TEXT NOT NULL DEFAULT 'agent',
    source_ref     TEXT NOT NULL DEFAULT '',
    trust_at_write REAL NOT NULL DEFAULT 1.0,
    delta          REAL NOT NULL DEFAULT 0.0,
    created_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- Look up a memory's corroboration ledger fast (aggregation inputs).
CREATE INDEX IF NOT EXISTS idx_corrob_memory ON memory_corroborations(memory_id);
-- Dedup guard: one corroboration per (memory, source) — a source corroborating
-- the same memory twice is idempotent, not double-counted.
CREATE UNIQUE INDEX IF NOT EXISTS idx_corrob_memory_source
    ON memory_corroborations(memory_id, source_kind, source_ref)
    WHERE delta > 0;
