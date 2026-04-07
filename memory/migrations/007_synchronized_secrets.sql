-- Migration 007: Synchronized Secrets Vault
-- Author: Gemini CLI
-- Description: Creates the encrypted vault table for synchronized secrets.

CREATE TABLE IF NOT EXISTS synchronized_secrets (
    service_name    TEXT PRIMARY KEY,
    encrypted_value TEXT NOT NULL,
    version         INTEGER DEFAULT 1,
    origin_device   TEXT,
    updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ss_updated ON synchronized_secrets(updated_at);
