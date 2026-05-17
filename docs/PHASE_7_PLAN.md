# Phase 7 Modularization & Optimization Plan

This document outlines the final steps to complete the modularization of the `m3-memory` core engine, focusing on extracting the remaining major components from `bin/memory_core.py` into the `bin/memory/` package, and implementing immediate Python-side performance optimizations.

## 1. Extract `graph.py` (De-risking Circular Dependencies)
- **Target Functions:** `_graph_neighbor_ids`, `_session_neighbor_ids`, `_entity_graph_neighbor_ids`, `_score_extra_rows`, `memory_graph_impl`.
- **Goal:** Move these graph traversal and scoring helpers from `memory_core.py` to `bin/memory/graph.py`.
- **Impact:** This breaks the circular dependency between `memory_core.py` and `bin/memory/search.py`. Once moved, `search.py` can import these functions directly, allowing us to remove them from the `_resolve_mc_callbacks()` runtime globals-binding hack.

## 2. Extract `enrich.py` (Isolating LLM Calls)
- **Target Functions:** `_auto_classify`, `_maybe_auto_title`, `_maybe_auto_entities`, `_try_enrich_or_enqueue`, `_enqueue_fact_enrichment`, `_run_fact_enricher`, `_write_fact_rows`.
- **Goal:** Isolate all SLM-based enrichment, classification, and queueing logic into `bin/memory/enrich.py`.
- **Impact:** Removes heavy LLM integration code from the core shim, making the data pipelines cleaner.

## 3. Extract `write.py` (Core Mutators)
- **Target Functions:** `memory_write_impl`, `memory_write_bulk_impl`, `_check_contradictions`.
- **Goal:** Move the primary mutation paths into `bin/memory/write.py`.
- **Impact:** Completes the modularization of `memory_core.py`, reducing it to a pure re-export shim and task/agent registry.

## 4. Mypyc Compilation (Low-Hanging Performance)
- **Target Modules:** `bin/memory/util.py` and `bin/memory/fts.py`.
- **Goal:** Configure `pyproject.toml` / `setup.py` (if applicable) to compile these stateless leaf modules using `mypyc`.
- **Impact:** Expected 2-3x speedup on pure Python operations like FTS string sanitization and batch cosine fallbacks, with zero architectural risk.

## 5. Rust Oxidation Roadmap (External)
- **Goal:** Document the next targets for the external `m3-core-rs` repository in `docs/OXIDATION_TODO.md`.
- **Targets:** 
  1. Oxidize the candidate-assembly loop in `memory_search_scored_impl`.
  2. Oxidize the 3-tier entity resolution logic in `entity.py`.
  3. Export GLiNER ONNX model for the NER backend.
