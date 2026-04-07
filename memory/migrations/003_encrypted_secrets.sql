CREATE TABLE IF NOT EXISTS synchronized_secrets (
    service_name TEXT PRIMARY KEY,
    encrypted_value TEXT NOT NULL,
    version INTEGER DEFAULT 1,
    origin_device TEXT,
    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
