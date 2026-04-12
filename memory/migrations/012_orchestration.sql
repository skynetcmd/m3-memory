-- 012_orchestration.sql
-- Adds agent registry, notifications queue, and durable task state.
-- Additive-only; safe to re-run (migrate_memory.py is idempotent on duplicate/exists).

-- ── A. Agent Registry ────────────────────────────────────────────────────────
-- Identity + presence. Distinct from agent_retention_policies (which is policy,
-- keyed by agent_id). No FK from other tables to avoid brittle deletes.
CREATE TABLE IF NOT EXISTS agents (
    agent_id       TEXT PRIMARY KEY,
    role           TEXT DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'active',
    capabilities   TEXT DEFAULT '[]',
    metadata_json  TEXT DEFAULT '{}',
    created_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    last_seen      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);
CREATE INDEX IF NOT EXISTS idx_agents_role   ON agents(role);

-- ── B. Notifications Queue ───────────────────────────────────────────────────
-- Lightweight poll-friendly event channel. Not a durable task state store.
CREATE TABLE IF NOT EXISTS notifications (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id       TEXT NOT NULL,
    kind           TEXT NOT NULL,
    payload_json   TEXT DEFAULT '{}',
    created_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    read_at        TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_notif_agent_unread
    ON notifications(agent_id, read_at, created_at);
CREATE INDEX IF NOT EXISTS idx_notif_agent_kind
    ON notifications(agent_id, kind, read_at);

-- ── C. Tasks ─────────────────────────────────────────────────────────────────
-- Durable workflow state. parent_task_id is a soft reference (no FK) so that
-- partial trees can exist and deletes never cascade unexpectedly.
CREATE TABLE IF NOT EXISTS tasks (
    id                 TEXT PRIMARY KEY,
    title              TEXT NOT NULL,
    description        TEXT DEFAULT '',
    state              TEXT NOT NULL DEFAULT 'pending',
    owner_agent        TEXT DEFAULT NULL,
    created_by         TEXT NOT NULL,
    parent_task_id     TEXT DEFAULT NULL,
    result_memory_id   TEXT DEFAULT NULL,
    metadata_json      TEXT DEFAULT '{}',
    created_at         TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at         TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    completed_at       TEXT DEFAULT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_owner_state ON tasks(owner_agent, state);
CREATE INDEX IF NOT EXISTS idx_tasks_parent      ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_state       ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_created_by  ON tasks(created_by);
