# Tool inventory index

_Generated 2026-07-03T20:00:04.201985+00:00._

Re-run `python bin/gen_tool_inventory.py` after changing any tool.
Entries whose `sha1` no longer matches the live file need re-validation.

| Tool | Summary | Private |
|---|---|---|
| bin/_task_runtime.py | _task_runtime — shared runtime setup for m3-memory scheduled-task entrypoints. | yes |
| [bin/agent_protocol.py](agent_protocol.md) | (no docstring) |  |
| [bin/ai-audit.sh](ai-audit.sh.md) | (no docstring) |  |
| [bin/ai_mechanic.py](ai_mechanic.md) | (no docstring) |  |
| [bin/augment_memory.py](augment_memory.md) | Offline post-ingest augmentation utilities for memory_items. |  |
| [bin/auth_utils.py](auth_utils.md) | (no docstring) |  |
| [bin/auto_route.py](auto_route.md) | auto_route — multi-signal retrieval branch decider. |  |
| [bin/backfill_content_hash.py](backfill_content_hash.md) | backfill_content_hash.py — populate memory_embeddings.content_hash on legacy rows. |  |
| [bin/batch_runner.py](batch_runner.md) | Provider-neutral batch-API runner protocol with Anthropic implementation. |  |
| bin/bench_memory.py | Memory system benchmark script. | yes |
| bin/benchmark_memory.py | Retrieval Quality Benchmark for M3 Memory System. | yes |
| [bin/build_kg_variant.py](build_kg_variant.md) | Build a KG-enriched variant from an existing source variant. |  |
| [bin/chatlog_config.py](chatlog_config.md) | chatlog_config.py — configuration resolver for the chat log subsystem. |  |
| [bin/chatlog_core.py](chatlog_core.md) | chatlog_core.py — the load-bearing module for the chat log subsystem. |  |
| [bin/chatlog_decay.py](chatlog_decay.md) | chatlog_decay — deterministic ephemeral-content decay for chatlog turns. |  |
| [bin/chatlog_embed_sweeper.py](chatlog_embed_sweeper.md) | chatlog_embed_sweeper.py — lazy embed chat log rows missing embeddings. |  |
| [bin/chatlog_ingest.py](chatlog_ingest.md) | chatlog_ingest.py — CLI that reads a host-agent transcript file and writes |  |
| [bin/chatlog_init.py](chatlog_init.md) | chatlog_init.py — interactive setup CLI for the chat log subsystem. |  |
| [bin/chatlog_prune.py](chatlog_prune.md) | chatlog_prune — aged noise pruning for chatlog turns. |  |
| [bin/chatlog_redaction.py](chatlog_redaction.md) | Optional secret-scrubbing for chat log entries. |  |
| [bin/chatlog_status.py](chatlog_status.md) | chatlog_status.py — single-call summary of the chat log subsystem state. |  |
| [bin/chatlog_status_line.py](chatlog_status_line.md) | chatlog_status_line.py — anomaly-only status line generator. |  |
| [bin/check_tool_catalog_drift.py](check_tool_catalog_drift.md) | Single source of truth for the tool-catalog pre-push drift gate. |  |
| [bin/chroma_health.py](chroma_health.md) | CLI script to report ChromaDB sync health metrics. |  |
| [bin/chroma_sync_cli.py](chroma_sync_cli.md) | CLI wrapper for ChromaDB bi-directional sync. |  |
| [bin/cleanup_logs.sh](cleanup_logs.sh.md) | (no docstring) |  |
| [bin/cli_kb_browse.py](cli_kb_browse.md) | cli_kb_browse.py — Browse knowledge base entries in rank (importance) order. |  |
| [bin/cli_knowledge.py](cli_knowledge.md) | (no docstring) |  |
| [bin/consolidate_beliefs.py](consolidate_beliefs.md) | Autonomous episodic->semantic belief consolidation (knowledge-maintenance P4). |  |
| [bin/curator_apply.py](curator_apply.md) | Deterministic apply of a curator plan — one entry point, no LLM in the loop. |  |
| [bin/custom_tool_bridge.py](custom_tool_bridge.md) | (no docstring) |  |
| [bin/dashboard_server.py](dashboard_server.md) | M3 Cognitive & Observability Portal. |  |
| [bin/debug_agent_bridge.py](debug_agent_bridge.md) | Debug Agent MCP Bridge — Autonomous debugging tools. |  |
| [bin/deep_sync.py](deep_sync.md) | (no docstring) |  |
| [bin/embed_agent_instructions.py](embed_agent_instructions.md) | One-shot script: embed AGENT_INSTRUCTIONS.md sections as searchable memory items. |  |
| [bin/embed_backfill.py](embed_backfill.md) | embed_backfill.py — fill in missing embeddings for memory_items rows. |  |
| [bin/embed_server.py](embed_server.md) | Local embedding server — OpenAI-compatible /v1/embeddings endpoint. |  |
| bin/embed_server_gpu.py | AMD GPU Optimized Embedding Proxy — delegates to llama-server.exe. | yes |
| [bin/embed_sweep_lib.py](embed_sweep_lib.md) | embed_sweep_lib — shared embed-loop helper for sweeper-style backfill tools. |  |
| [bin/embedding_utils.py](embedding_utils.md) | Shared embedding and vector-math utilities for MCP bridges. |  |
| [bin/enrichment_state.py](enrichment_state.md) | Durable per-group enrichment state for m3_enrich. |  |
| [bin/fetch_sovereign_assets.py](fetch_sovereign_assets.md) | fetch_sovereign_assets.py — Hydrate the _assets/embedder directory for sovereign setup. |  |
| [bin/gen_capability_matrix.py](gen_capability_matrix.md) | gen_capability_matrix.py — generate docs/CAPABILITY_MATRIX.md from the MCP catalog. |  |
| [bin/gen_features_json.py](gen_features_json.md) | gen_features_json.py — generate docs/features.json (machine-readable capabilities). |  |
| [bin/gen_mcp_inventory.py](gen_mcp_inventory.md) | gen_mcp_inventory.py — Generates docs/MCP_TOOLS.md from mcp_tool_catalog and mcp_proxy. |  |
| [bin/gen_tool_manifest.py](gen_tool_manifest.md) | Generate a machine-readable tool-catalog manifest at docs/tools/MCP_CATALOG.json. |  |
| [bin/generate_configs.py](generate_configs.md) | (no docstring) |  |
| [bin/governor_cli.py](governor_cli.md) | `m3 governor <status\|migrate>` — inspect and migrate legacy scheduled tasks |  |
| [bin/grok_bridge.py](grok_bridge.md) | (no docstring) |  |
| [bin/homecoming.py](homecoming.md) | bin/homecoming.py — "Homecoming" migration script for m3-memory. |  |
| [bin/install_schedules.py](install_schedules.md) | M3 Memory: Cross-Platform Schedule Installer. |  |
| [bin/install_wolfssl.py](install_wolfssl.md) | install_wolfssl.py — build the OPEN-SOURCE wolfSSL library from official |  |
| [bin/llm_failover.py](llm_failover.md) | LLM Failover Module |  |
| [bin/m3_autoenrich.py](m3_autoenrich.md) | Toggle the M3_AUTO_ENRICH env var on/off, cross-platform. |  |
| [bin/m3_chatlog_backfill_embed.py](m3_chatlog_backfill_embed.md) | m3_chatlog_backfill_embed — Embed unembedded rows in core memory + chatlog. |  |
| [bin/m3_chatlog_backfill_title.py](m3_chatlog_backfill_title.md) | m3_chatlog_backfill_title — Backfill missing/useless titles from content. |  |
| [bin/m3_chatlog_enrich_backfill.py](m3_chatlog_enrich_backfill.md) | Backfill `observation_queue` from existing chatlog rows. |  |
| [bin/m3_cognitive_loop.py](m3_cognitive_loop.md) | m3_cognitive_loop — The autonomous heartbeat of m3-memory. |  |
| [bin/m3_enrich.py](m3_enrich.md) | m3_enrich — User-facing enrichment CLI for core memory + chatlogs. |  |
| [bin/m3_enrich_assign.py](m3_enrich_assign.md) | m3_enrich_assign.py — assign enrichment_groups.send_to for routed runs. |  |
| [bin/m3_enrich_batch.py](m3_enrich_batch.md) | m3-enrich-batch — async/batch variant of bin/m3_enrich.py. |  |
| [bin/m3_enrich_batch_parallel.py](m3_enrich_batch_parallel.md) | m3_enrich_batch_parallel — launch N pipelined batch workers against |  |
| [bin/m3_enrich_report.py](m3_enrich_report.md) | Summarize an m3_enrich run from enrichment_groups + enrichment_runs. |  |
| [bin/m3_entities.py](m3_entities.md) | m3_entities — build entity-graph rows from your core/chatlog DBs. |  |
| [bin/m3_entities_gliner.py](m3_entities_gliner.md) | m3_entities_gliner — fast local entity extraction via GLiNER (zero-shot NER). |  |
| [bin/m3_lifecycle_summary.py](m3_lifecycle_summary.md) | CLI wrapper for the memory lifecycle/contradiction observability summary. |  |
| [bin/m3_sdk.py](m3_sdk.md) | m3_sdk — facade. Real implementations live in bin/m3_core/*. |  |
| bin/macbook_status_server.py | MacBook network & LM Studio status server for Homepage dashboard. | yes |
| [bin/mcp_proxy.py](mcp_proxy.md) | MCP Tool Execution Proxy  v2.0 |  |
| [bin/mcp_tool_catalog.py](mcp_tool_catalog.md) | mcp_tool_catalog.py — single source of truth for the m3-memory MCP tool catalog. |  |
| [bin/measure_tool_tokens.py](measure_tool_tokens.md) | measure_tool_tokens.py — quantify token cost of MCP tool schemas. |  |
| [bin/memory_bridge.py](memory_bridge.md) | (no docstring) |  |
| [bin/memory_core.py](memory_core.md) | Core memory primitives: single + bulk write, search, enrichment, emitters. |  |
| [bin/memory_doctor.py](memory_doctor.md) | m3-memory doctor — thin CLI dispatcher over the doctor phases. |  |
| [bin/memory_maintenance.py](memory_maintenance.md) | (no docstring) |  |
| [bin/memory_sync.py](memory_sync.md) | (no docstring) |  |
| [bin/migrate_entity_vocab.py](migrate_entity_vocab.md) | One-shot migration: rename v1 entity vocabulary to v2-aligned names. |  |
| [bin/migrate_flat_memory.py](migrate_flat_memory.md) | migrate_flat_memory.py — one-way ETL from flat-file / SQLite agent memory |  |
| [bin/migrate_memory.py](migrate_memory.md) | Migration runner for the m3-memory SQLite databases. |  |
| bin/mission_control.py | mission_control.py — Cross-platform pulse dashboard (macOS / Windows / Linux). | yes |
| [bin/news_fetcher.py](news_fetcher.md) | (no docstring) |  |
| [bin/pg_setup.py](pg_setup.md) | (no docstring) |  |
| [bin/pg_sync.py](pg_sync.md) | (no docstring) |  |
| [bin/pg_sync.sh](pg_sync.sh.md) | (no docstring) |  |
| [bin/promote_pipeline.py](promote_pipeline.md) | LLM-judged promotion pipeline: tightened candidate selection + SLM judge. |  |
| [bin/re_embed_all.py](re_embed_all.md) | (no docstring) |  |
| [bin/release_orphan_claims.py](release_orphan_claims.md) | release_orphan_claims — safely release stuck in_progress enrichment_groups rows. |  |
| [bin/run_observer.py](run_observer.md) | Phase D Mastra-style Observer drainer. |  |
| [bin/run_reflector.py](run_reflector.md) | Phase D Mastra-style Reflector drainer. |  |
| [bin/secret_rotator.py](secret_rotator.md) | (no docstring) |  |
| [bin/session_handoff.py](session_handoff.md) | (no docstring) |  |
| [bin/setup_hooks.py](setup_hooks.md) | Enable the repo's shared git hooks for this clone. |  |
| [bin/setup_secret.py](setup_secret.md) | Interactive CLI for adding API keys to the m3-memory encrypted vault. |  |
| [bin/setup_test_db.py](setup_test_db.md) | Seed a fresh SQLite DB with the full m3-memory schema for test isolation. |  |
| [bin/slm_intent.py](slm_intent.md) | Small-Language-Model intent classifier with named-profile configs. |  |
| [bin/split_chatlog_from_core.py](split_chatlog_from_core.md) | split_chatlog_from_core — move chat_log rows out of the CORE memory DB into |  |
| [bin/start_mcp_proxy.sh](start_mcp_proxy.sh.md) | start_mcp_proxy.sh — Launch the MCP Tool Execution Proxy on localhost:9000 |  |
| [bin/statusline-command.sh](statusline-command.sh.md) | (no docstring) |  |
| [bin/sync_all.py](sync_all.md) | sync_all.py — Hourly sync runner (SQLite <-> PostgreSQL + ChromaDB). |  |
| [bin/temporal_utils.py](temporal_utils.md) | Enhanced temporal resolution utility for m3-memory. |  |
| [bin/test_bulk_parity.py](test_bulk_parity.md) | Real integration tests for memory_write_bulk_impl. |  |
| [bin/test_debug_agent.py](test_debug_agent.md) | End-to-end test suite for debug_agent_bridge.py. |  |
| [bin/test_fips_integrity.py](test_fips_integrity.md) | test_fips_integrity.py — Validation suite for FIPS-ready crypto abstraction. |  |
| [bin/test_keychain.py](test_keychain.md) | (no docstring) |  |
| [bin/test_knowledge.py](test_knowledge.md) | (no docstring) |  |
| [bin/test_mcp_proxy.py](test_mcp_proxy.md) | test_mcp_proxy.py — End-to-end proxy test suite |  |
| [bin/test_mcp_proxy_unit.py](test_mcp_proxy_unit.md) | test_mcp_proxy_unit.py - In-process unit tests for mcp_proxy. |  |
| [bin/test_memory_bridge.py](test_memory_bridge.md) | End-to-end test suite for memory_bridge.py. |  |
| [bin/test_unified_router.py](test_unified_router.md) | (no docstring) |  |
| [bin/thermal_utils.py](thermal_utils.md) | (no docstring) |  |
| [bin/unified_ai.py](unified_ai.md) | Unified chat client across Gemini, Claude, and LM Studio. |  |
| [bin/web_research_bridge.py](web_research_bridge.md) | (no docstring) |  |
| [bin/weekly_auditor.py](weekly_auditor.md) | Weekly Audit Report -- M3 Memory |  |
| [install_os.py](install_os.md) | (no docstring) |  |
| [run_tests.py](run_tests.md) | (no docstring) |  |
| [scan_repo_v7.py](scan_repo_v7.md) | Scan orchestrator for the m3-memory security pipeline on LXC 504. |  |
| [scripts/inventory_graph.py](inventory_graph.md) | Build a mermaid call-graph from tool-inventory markdown files. |  |
| [scripts/metadata_filler.py](metadata_filler.md) | (no docstring) |  |
| [scripts/test_focus_fix.py](test_focus_fix.md) | (no docstring) |  |
| [validate_env.py](validate_env.md) | (no docstring) |  |
