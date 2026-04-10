# Changelog

All notable changes to M3 Memory are documented here.

---

## [2026.04.06] — April 6, 2026 — Production Release

### Added
- **Conversation summarization** — `conversation_summarize` compresses long threads into 3-5 key points via local LLM
- **LLM auto-classification** — `type="auto"` lets the local LLM categorize memories into one of 18 types
- **Explainable search** — `memory_suggest` returns full score breakdowns (vector + BM25 + MMR penalty) per result
- **Multi-layered consolidation** — `memory_consolidate` merges old memory groups into LLM-generated summaries
- **Portable export/import** — JSON round-trip backup including embeddings and relationships
- **Retrieval benchmarks** — `bin/bench_memory.py` measures MRR, Hit@k, and latency
- **Configurable thresholds** — `DEDUP_LIMIT`, `DEDUP_THRESHOLD`, `CONTRADICTION_THRESHOLD`, `SEARCH_ROW_CAP` via env vars
- **25 MCP tools** — full tool set across memory ops, knowledge graph, conversations, lifecycle, data governance, and operations

### Fixed
- Search recursion bug in `memory_search_impl` — FTS-to-semantic fallback was incorrectly passing state into bitemporal filter parameters
- `memory_export` excluded non-existent `metadata_json` column from `memory_relationships`
- LM Studio connectivity checks standardized to `localhost` with proper API tokens

### Changed
- `VALID_MEMORY_TYPES` expanded to include `auto`
- `VALID_RELATIONSHIP_TYPES` expanded to include `consolidates`
- AES-256 vault upgraded to PBKDF2 600K iterations (auto-migrates legacy 100K secrets on first decryption)

### Test Coverage
- 41 end-to-end tests passing across all features
- Retrieval MRR 1.0 achieved in standardized benchmarks
- CI: lint (Ruff) + typecheck (Mypy) + pytest on Ubuntu/macOS/Windows × Python 3.11/3.12

---

For the full technical history see [docs/CHANGELOG_2026.md](./docs/CHANGELOG_2026.md).
