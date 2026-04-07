# M3 Memory System Changelog - 2026

## April 6, 2026

### ✨ New Features

#### Conversation Summarization
- Implemented `conversation_summarize_impl` in `bin/memory_core.py`.
- Automated summarization of long conversations into 3-5 key points using local LLM inference.
- New MCP tool: `conversation_summarize(conversation_id, threshold=20)`.
- Summaries are stored as `summary` type and linked via `references` relationship.

#### Tier 5 Implementation Complete
- **LLM Auto-Classification**: Intelligent categorization of memories using LLM inference (enabled via `type='auto'`).
- **Explainability (memory_suggest)**: New search mode providing detailed scoring breakdowns (Vector + BM25 + MMR penalty).
- **Multi-layered Consolidation**: Background summarization of old memories by type/agent to reduce clutter while preserving knowledge.
- **Portable Export/Import**: JSON-based backup and restoration of memories, including embeddings and relationships.
- **Retrieval Benchmarks**: New `bin/benchmark_memory.py` utility for measuring retrieval quality (MRR, Hit@N, Latency).
- **Configurable Limits**: Moved hardcoded thresholds to environment variables:
  - `DEDUP_LIMIT` (default 1000)
  - `DEDUP_THRESHOLD` (default 0.92)
  - `CONTRADICTION_THRESHOLD` (default 0.85)
  - `SEARCH_ROW_CAP` (default 500)

### 🐛 Bug Fixes
- **Search Recursion Fix**: Resolved a critical bug in `memory_search_impl` where recursion for FTS-to-semantic fallback was incorrectly passing state into bitemporal filter parameters.
- **Relationship Schema**: Fixed `memory_export` to exclude non-existent `metadata_json` column from `memory_relationships`.
- **Benchmark Reliability**: Standardized LM Studio connectivity checks to use `localhost` and proper API tokens.

### 📚 Documentation & Architecture
- Updated `ARCHITECTURE.md` with 6 new Tier 5 tools.
- Expanded `VALID_MEMORY_TYPES` to include `auto`.
- Added `consolidates` to `VALID_RELATIONSHIP_TYPES`.

### ✅ Verification
- **Test Suite**: 161/161 tests passing in `bin/test_memory_bridge.py`.
- **Quality**: Retrieval MRR 1.0 achieved in standardized benchmarks.
