-- 019_chat_log.up.sql
-- Partial indexes scoped to type='chat_log' so they stay small and cheap.
-- No schema change; chat_log uses the existing memory_items shape. The
-- provenance fields (host_agent, provider, model_id, turn_index, tokens_in,
-- tokens_out, cost_usd, latency_ms, redacted, redaction_count,
-- original_content_sha256) all live in metadata_json.

CREATE INDEX IF NOT EXISTS idx_memory_items_chat_log
    ON memory_items (conversation_id, created_at)
    WHERE type='chat_log' AND is_deleted=0;

CREATE INDEX IF NOT EXISTS idx_memory_items_host_agent
    ON memory_items (json_extract(metadata_json,'$.host_agent'))
    WHERE type='chat_log';

CREATE INDEX IF NOT EXISTS idx_memory_items_provider
    ON memory_items (json_extract(metadata_json,'$.provider'))
    WHERE type='chat_log';

CREATE INDEX IF NOT EXISTS idx_memory_items_model_id
    ON memory_items (json_extract(metadata_json,'$.model_id'))
    WHERE type='chat_log';

CREATE INDEX IF NOT EXISTS idx_memory_items_provider_time
    ON memory_items (json_extract(metadata_json,'$.provider'), created_at)
    WHERE type='chat_log' AND is_deleted=0;
