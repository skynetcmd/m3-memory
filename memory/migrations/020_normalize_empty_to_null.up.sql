-- 020_normalize_empty_to_null.up.sql
--
-- The write paths in memory_core.py historically stored empty strings for
-- optional nullable columns (variant, valid_to, valid_from) when callers
-- didn't supply values. The read paths — specifically the variant filter
-- in memory_search_scored_impl — treat "no value" as SQL NULL, so rows
-- written with "" were silently hidden from the default search when the
-- validator applied variant="__none__" → "variant IS NULL".
--
-- That disagreement was fixed at the write path (commits a90d444 and
-- 9e57c59 coerce "" → None before INSERT). This migration normalizes
-- pre-existing historical rows so the columns are uniformly NULL and
-- any future tightened read predicate won't silently drop rows.
--
-- Safe to re-run (UPDATE with WHERE = '' is idempotent — empty-string rows
-- become NULL on first pass, subsequent runs match nothing).

UPDATE memory_items SET variant = NULL WHERE variant = '';
UPDATE memory_items SET valid_to = NULL WHERE valid_to = '';
UPDATE memory_items SET valid_from = NULL WHERE valid_from = '';
