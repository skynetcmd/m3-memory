-- 010_tier_features.sql
-- Adds columns for bitemporal model, content signing, agent retention policies, GDPR.
-- All statements are additive — safe to re-run.

-- ── Bitemporal Model ──────────────────────────────────────────────────────────
-- Track when facts were actually true (not just when stored).
-- valid_from/valid_to enable point-in-time queries ("what did we know as of X?").

ALTER TABLE memory_items ADD COLUMN valid_from TEXT DEFAULT '';
ALTER TABLE memory_items ADD COLUMN valid_to TEXT DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_mi_valid_from ON memory_items(valid_from);

-- ── Content Integrity ─────────────────────────────────────────────────────────
-- SHA-256 hash of content for tamper detection.

ALTER TABLE memory_items ADD COLUMN content_hash TEXT DEFAULT '';

-- ── Agent Retention Policies ──────────────────────────────────────────────────
-- Per-agent memory limits and TTLs. Enforced by memory_maintenance.

CREATE TABLE IF NOT EXISTS agent_retention_policies (
    agent_id        TEXT PRIMARY KEY,
    max_memories    INTEGER DEFAULT 1000,
    ttl_days        INTEGER DEFAULT 0,
    auto_archive    INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);

-- ── GDPR Data Subject Requests ────────────────────────────────────────────────
-- Audit log for right-to-be-forgotten and data export requests.

CREATE TABLE IF NOT EXISTS gdpr_requests (
    id              TEXT PRIMARY KEY,
    subject_id      TEXT NOT NULL,
    request_type    TEXT NOT NULL,
    status          TEXT DEFAULT 'pending',
    items_affected  INTEGER DEFAULT 0,
    requested_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_gdpr_subject ON gdpr_requests(subject_id);
