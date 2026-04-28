-- 025_observation_queue.up.sql
--
-- Phase D Mastra Observer + Reflector pipeline. Two queues:
--
-- observation_queue: rows enqueued on conversation-close, drained by
-- bin/run_observer.py which calls the Observer SLM, parses
-- {observations: [...]}, and writes type='observation' rows with
-- three-date metadata (created_at=observation_date,
-- valid_from=referenced_date, metadata_json.relative_date,
-- metadata_json.supersedes_hint).
--
-- reflector_queue: rows enqueued when the per-(user_id, conversation_id)
-- observation count exceeds M3_REFLECTOR_THRESHOLD (default 50). Drained
-- by bin/run_reflector.py which calls the Reflector SLM with
-- {existing, new}, parses {observations, supersedes}, and translates the
-- supersedes list into memory_link_impl(relationship_type='supersedes')
-- edges.
--
-- Mirrors fact_enrichment_queue (migration 023) shape — same backoff /
-- retry semantics, same UNIQUE-on-key dedup. observation_queue keys on
-- conversation_id (one Observer call per conversation, not per turn);
-- reflector_queue keys on (user_id, conversation_id) (one Reflector call
-- per group when threshold trips).
--
-- The type='observation' index supports retrieval-time partition into
-- obs_hits vs raw_hits when M3_PREFER_OBSERVATIONS=1 fires.
--
-- Hardening: all operations use IF NOT EXISTS to ensure idempotence.

CREATE TABLE IF NOT EXISTS observation_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    user_id         TEXT,
    enqueued_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT,
    last_attempt_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_oq_conversation_id ON observation_queue(conversation_id);
CREATE INDEX IF NOT EXISTS idx_oq_attempts ON observation_queue(attempts, enqueued_at);

CREATE TABLE IF NOT EXISTS reflector_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    user_id         TEXT,
    obs_count_at_enqueue INTEGER,
    enqueued_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT,
    last_attempt_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rq_user_conv ON reflector_queue(user_id, conversation_id);
CREATE INDEX IF NOT EXISTS idx_rq_attempts ON reflector_queue(attempts, enqueued_at);

-- Retrieval helper: fast partition of memory_items into observations vs everything else.
-- Used by memory_search_scored_impl when M3_PREFER_OBSERVATIONS=1 to do the
-- post-rank obs_hits / raw_hits split without scanning every memory_items row.
CREATE INDEX IF NOT EXISTS idx_mi_type_user_obs
  ON memory_items(type, user_id, valid_from)
  WHERE type='observation';
