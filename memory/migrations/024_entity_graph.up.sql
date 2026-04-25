-- 024_entity_graph.up.sql
--
-- Create the entity-relation graph schema for optional entity extraction
-- and relationship linking. Mirrors the fact_enrichment_queue pattern:
-- entities table holds canonical entities, memory_item_entities links memories
-- to entities with mention metadata, entity_relationships tracks typed edges,
-- and entity_extraction_queue holds work pending SLM-driven extraction.
--
-- FOREIGN KEY constraints: cascade on memory_items deletion to maintain
-- referential integrity. source_memory_id on entity_relationships uses SET NULL
-- so relationship edges persist even if the source memory is deleted.
--
-- Hardening: all operations use IF NOT EXISTS to ensure idempotence
-- on already-migrated DBs.

CREATE TABLE IF NOT EXISTS entities (
    id              TEXT PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    entity_type     TEXT NOT NULL,
    attributes_json TEXT DEFAULT '{}',
    valid_from      TEXT,
    valid_to        TEXT,
    content_hash    TEXT,
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_entities_canonical_type ON entities(canonical_name, entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_type           ON entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_entities_hash           ON entities(content_hash);

CREATE TABLE IF NOT EXISTS memory_item_entities (
    memory_id       TEXT NOT NULL,
    entity_id       TEXT NOT NULL,
    mention_text    TEXT,
    mention_offset  INTEGER DEFAULT 0,
    confidence      REAL DEFAULT 0.85,
    created_at      TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    PRIMARY KEY (memory_id, entity_id, mention_offset),
    FOREIGN KEY (memory_id) REFERENCES memory_items(id) ON DELETE CASCADE,
    FOREIGN KEY (entity_id) REFERENCES entities(id)     ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_mie_entity ON memory_item_entities(entity_id);

CREATE TABLE IF NOT EXISTS entity_relationships (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    from_entity      TEXT NOT NULL,
    to_entity        TEXT NOT NULL,
    predicate        TEXT NOT NULL,
    confidence       REAL DEFAULT 0.85,
    valid_from       TEXT,
    valid_to         TEXT,
    source_memory_id TEXT,
    created_at       TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    FOREIGN KEY (from_entity)      REFERENCES entities(id) ON DELETE CASCADE,
    FOREIGN KEY (to_entity)        REFERENCES entities(id) ON DELETE CASCADE,
    FOREIGN KEY (source_memory_id) REFERENCES memory_items(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_er_from      ON entity_relationships(from_entity, predicate);
CREATE INDEX IF NOT EXISTS idx_er_to        ON entity_relationships(to_entity, predicate);
CREATE INDEX IF NOT EXISTS idx_er_predicate ON entity_relationships(predicate);

CREATE TABLE IF NOT EXISTS entity_extraction_queue (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    memory_id       TEXT NOT NULL,
    enqueued_at     TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT,
    last_attempt_at TEXT,
    FOREIGN KEY (memory_id) REFERENCES memory_items(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_eeq_memory_id ON entity_extraction_queue(memory_id);
CREATE INDEX IF NOT EXISTS idx_eeq_attempts ON entity_extraction_queue(attempts, enqueued_at);
