-- 040_drop_chroma_sync_tables.up.sql
-- Retires the ChromaDB federation/sync feature. Chroma was an optional L3 sync
-- backend that mirrored embeddings to a remote Chroma instance and pulled
-- federated results back. It was never on the vector-search critical path — both
-- SQLite (sqlite-vec/Rust cosine) and PostgreSQL (BYTEA + Rust cosine, pgvector
-- as the native ANN accelerator) do vector search without it — so it carried a
-- sync pipeline, a data-duplication cost, and a second system to run for no
-- capability the native backends don't already provide. Removed wholesale.
--
-- Forward-only: migrations 001/005 that CREATE these tables are immutable applied
-- history and are left untouched. This migration DROPs the now-dead tables:
--   - chroma_sync_queue         (write-side enqueue of memory_ids to sync)
--   - chroma_mirror             (local mirror of items pulled from remote Chroma)
--   - chroma_mirror_embeddings  (embeddings for mirrored items; FK -> chroma_mirror)
--   - sync_conflicts            (local/remote conflict ledger for the sync worker)
--   - sync_state                (per-collection last_pull/last_push cursors)
--
-- Their indexes (idx_csq_*, idx_cm_*, idx_sc_*) drop with the tables in SQLite.
-- Any deployment that never enabled Chroma had these sitting empty; a deployment
-- that did use it loses only the sync scaffolding — the durable memories live in
-- memory_items/memory_embeddings and are unaffected.

DROP TABLE IF EXISTS chroma_mirror_embeddings;
DROP TABLE IF EXISTS chroma_mirror;
DROP TABLE IF EXISTS chroma_sync_queue;
DROP TABLE IF EXISTS sync_conflicts;
DROP TABLE IF EXISTS sync_state;
