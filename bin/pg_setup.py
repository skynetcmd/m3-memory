import psycopg2
import logging
import os
import sys
from auth_utils import get_api_key

logging.basicConfig(level=logging.INFO, format='%(name)s: [%(levelname)s] %(message)s')
logger = logging.getLogger("pg_setup")

def _get_pg_url() -> str:
    """Resolve PostgreSQL connection URL from environment or encrypted vault."""
    url = os.getenv("PG_URL", "").strip()
    if url:
        return url
    url = get_api_key("PG_URL")
    if url:
        return url
    python_cmd = "python" if os.name == "nt" else "python3"
    logger.error(f"PG_URL not found. Set PG_URL env var or store it via: {python_cmd} -c \"from auth_utils import set_api_key; set_api_key('PG_URL', 'postgresql://USERNAME:REPLACE_WITH_YOUR_PASSWORD@host:5432/db')\"")
    sys.exit(1)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS activity_logs (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    query TEXT,
    response TEXT,
    model_used TEXT DEFAULT 'DeepSeek-R1-70B'
);

CREATE TABLE IF NOT EXISTS project_decisions (
    id SERIAL PRIMARY KEY,
    project TEXT,
    decision TEXT,
    rationale TEXT,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS hardware_specs (
    id SERIAL PRIMARY KEY,
    component TEXT,
    spec TEXT,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_focus (
    id INTEGER PRIMARY KEY,
    summary TEXT,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS memory_items (
    id            TEXT PRIMARY KEY,
    type          TEXT NOT NULL,
    title         TEXT,
    content       TEXT,
    metadata_json JSONB,
    agent_id      TEXT,
    model_id      TEXT,
    change_agent  TEXT DEFAULT 'unknown',
    importance    REAL DEFAULT 0.5,
    source        TEXT DEFAULT 'agent',
    origin_device TEXT DEFAULT 'macbook',
    is_deleted    INTEGER DEFAULT 0,
    expires_at    TIMESTAMP WITH TIME ZONE,
    decay_rate    REAL DEFAULT 0.0,
    created_at    TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP WITH TIME ZONE
);

CREATE TABLE IF NOT EXISTS memory_embeddings (
    id          TEXT PRIMARY KEY,
    memory_id   TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    embedding   BYTEA NOT NULL,
    embed_model TEXT DEFAULT 'jina-embeddings-v5',
    dim         INTEGER DEFAULT 1024,
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS memory_relationships (
    id                TEXT PRIMARY KEY,
    from_id           TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    to_id             TEXT NOT NULL REFERENCES memory_items(id) ON DELETE CASCADE,
    relationship_type TEXT NOT NULL,
    created_at        TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_mi_type       ON memory_items(type);
CREATE INDEX IF NOT EXISTS idx_mi_agent      ON memory_items(agent_id);
CREATE INDEX IF NOT EXISTS idx_mi_model      ON memory_items(model_id);
CREATE INDEX IF NOT EXISTS idx_mi_created    ON memory_items(created_at);
CREATE INDEX IF NOT EXISTS idx_mi_deleted    ON memory_items(is_deleted);
CREATE INDEX IF NOT EXISTS idx_me_memory_id  ON memory_embeddings(memory_id);
CREATE INDEX IF NOT EXISTS idx_mr_from       ON memory_relationships(from_id);
CREATE INDEX IF NOT EXISTS idx_mr_to         ON memory_relationships(to_id);
"""

def main():
    try:
        pg_url = _get_pg_url()
        conn = psycopg2.connect(pg_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            logger.info("Applying PostgreSQL schema to data warehouse...")
            cur.execute(SCHEMA_SQL)
        logger.info("Schema applied successfully!")
        conn.close()
    except Exception as e:
        logger.error(f"Failed to setup PostgreSQL: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
