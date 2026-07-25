-- pg_004_files_fts.down.sql
-- Reverse of pg_004_files_fts.up.sql: drop the GIN indexes and the generated
-- search_vector columns on files.leaves and files.file_nodes.
DROP INDEX IF EXISTS files.idx_file_nodes_search_vector;
DROP INDEX IF EXISTS files.idx_leaves_search_vector;
ALTER TABLE files.file_nodes DROP COLUMN IF EXISTS search_vector;
ALTER TABLE files.leaves     DROP COLUMN IF EXISTS search_vector;
