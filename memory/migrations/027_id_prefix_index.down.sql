-- 027_id_prefix_index.down.sql
--
-- Reverses migration 027 by dropping the SUBSTR(id,1,8) expression index
-- on memory_items. Safe to run if the index never existed.

DROP INDEX IF EXISTS idx_mi_id_prefix8;
