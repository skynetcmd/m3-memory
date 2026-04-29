-- 029_enrichment_content_size.down.sql

DROP INDEX IF EXISTS idx_eg_size;
ALTER TABLE enrichment_groups DROP COLUMN content_size_k;
