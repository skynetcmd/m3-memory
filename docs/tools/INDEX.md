# Tool inventory index

_Generated 2026-04-22T02:12:39.440550+00:00._

Re-run `python bin/gen_tool_inventory.py` after changing any tool.
Entries whose `sha1` no longer matches the live file need re-validation.

| Tool | Summary | Private |
|---|---|---|
| [benchmarks/locomo/analyze_handoff.py](analyze_handoff.md) | Phase 1 analysis: what does retrieval hand off to the answerer? |  |
| [benchmarks/locomo/analyze_prompt.py](analyze_prompt.md) | Phase 1: answerer-prompt anatomy and waste analysis. |  |
| [benchmarks/locomo/bench_locomo.py](bench_locomo.md) | Dialog-QA benchmark runner for m3-memory. |  |
| [benchmarks/locomo/compare_runs.py](compare_runs.md) | Compare two Phase 1 runs side-by-side. |  |
| [benchmarks/locomo/join_variant_reports.py](join_variant_reports.md) | Join multiple retrieval_audit summary.json files into one comparison report. |  |
| [benchmarks/locomo/probe_ingest_cost.py](probe_ingest_cost.md) | Measure ingestion cost per variant for 1 and 10 LOCOMO turns. |  |
| [benchmarks/locomo/probe_issues.py](probe_issues.md) | Probe specific issues identified in handoff analysis: |  |
| [benchmarks/locomo/reingest.py](reingest.md) | Re-ingest LOCOMO samples with explicit variant tags. |  |
| [benchmarks/locomo/retrieval_audit.py](retrieval_audit.md) | Phase 1: LOCOMO retrieval audit. |  |
| [benchmarks/locomo/stamp_variants_from_chainlog.py](stamp_variants_from_chainlog.md) | Retrofit `variant` field into summary.json files based on a chain.log. |  |
| [benchmarks/longmemeval/bench_longmemeval.py](bench_longmemeval.md) | Long-session QA benchmark runner for m3-memory. |  |
| [bin/agent_protocol.py](agent_protocol.md) | (no docstring) |  |
| [bin/ai_mechanic.py](ai_mechanic.md) | (no docstring) |  |
| [bin/augment_memory.py](augment_memory.md) | Offline post-ingest augmentation utilities for memory_items. |  |
| [bin/auth_utils.py](auth_utils.md) | (no docstring) |  |
| [bin/bench_memory.py](bench_memory.md) | Memory system benchmark script. |  |
| [bin/benchmark_memory.py](benchmark_memory.md) | Retrieval Quality Benchmark for M3 Memory System. |  |
| [bin/build_kg_variant.py](build_kg_variant.md) | Build a KG-enriched variant from an existing source variant. |  |
| [bin/chatlog_config.py](chatlog_config.md) | chatlog_config.py — configuration resolver for the chat log subsystem. |  |
| [bin/chatlog_core.py](chatlog_core.md) | chatlog_core.py — the load-bearing module for the chat log subsystem. |  |
| [bin/chatlog_embed_sweeper.py](chatlog_embed_sweeper.md) | chatlog_embed_sweeper.py — lazy embed chat log rows missing embeddings. |  |
| [bin/chatlog_ingest.py](chatlog_ingest.md) | chatlog_ingest.py — CLI that reads a host-agent transcript file and writes |  |
| [bin/chatlog_init.py](chatlog_init.md) | chatlog_init.py — interactive setup CLI for the chat log subsystem. |  |
| [bin/chatlog_redaction.py](chatlog_redaction.md) | Optional secret-scrubbing for chat log entries. |  |
| [bin/chatlog_status.py](chatlog_status.md) | chatlog_status.py — single-call summary of the chat log subsystem state. |  |
| [bin/chatlog_status_line.py](chatlog_status_line.md) | chatlog_status_line.py — anomaly-only status line generator. |  |
| [bin/chroma_sync_cli.py](chroma_sync_cli.md) | CLI wrapper for ChromaDB bi-directional sync. |  |
| [bin/cli_kb_browse.py](cli_kb_browse.md) | cli_kb_browse.py — Browse knowledge base entries in rank (importance) order. |  |
| [bin/cli_knowledge.py](cli_knowledge.md) | (no docstring) |  |
| [bin/custom_tool_bridge.py](custom_tool_bridge.md) | (no docstring) |  |
| [bin/debug_agent_bridge.py](debug_agent_bridge.md) | Debug Agent MCP Bridge — Autonomous debugging tools. |  |
| [bin/deep_sync.py](deep_sync.md) | (no docstring) |  |
| [bin/embed_agent_instructions.py](embed_agent_instructions.md) | One-shot script: embed AGENT_INSTRUCTIONS.md sections as searchable memory items. |  |
| [bin/embed_server.py](embed_server.md) | Local embedding server — OpenAI-compatible /v1/embeddings endpoint. |  |
| [bin/embed_server_gpu.py](embed_server_gpu.md) | AMD GPU Optimized Embedding Proxy — delegates to llama-server.exe. | yes |
| [bin/embedding_utils.py](embedding_utils.md) | Shared embedding and vector-math utilities for MCP bridges. |  |
| [bin/gen_mcp_inventory.py](gen_mcp_inventory.md) | gen_mcp_inventory.py — Generates docs/MCP_TOOLS.md from mcp_tool_catalog and mcp_proxy. |  |
| [bin/generate_configs.py](generate_configs.md) | (no docstring) |  |
| [bin/grok_bridge.py](grok_bridge.md) | (no docstring) |  |
| [bin/install_schedules.py](install_schedules.md) | M3 Memory: Cross-Platform Schedule Installer. |  |
| [bin/llm_failover.py](llm_failover.md) | LLM Failover Module |  |
| [bin/m3_sdk.py](m3_sdk.md) | (no docstring) |  |
| [bin/macbook_status_server.py](macbook_status_server.md) | MacBook network & LM Studio status server for Homepage dashboard. | yes |
| [bin/mcp_proxy.py](mcp_proxy.md) | MCP Tool Execution Proxy  v2.0 |  |
| [bin/mcp_tool_catalog.py](mcp_tool_catalog.md) | mcp_tool_catalog.py — single source of truth for the m3-memory MCP tool catalog. |  |
| [bin/memory_bridge.py](memory_bridge.md) | (no docstring) |  |
| [bin/memory_core.py](memory_core.md) | Core memory primitives: single + bulk write, search, enrichment, emitters. |  |
| [bin/memory_doctor.py](memory_doctor.md) | (no docstring) |  |
| [bin/memory_maintenance.py](memory_maintenance.md) | (no docstring) |  |
| [bin/memory_sync.py](memory_sync.md) | (no docstring) |  |
| [bin/migrate_flat_memory.py](migrate_flat_memory.md) | migrate_flat_memory.py — one-way ETL from flat-file / SQLite agent memory |  |
| [bin/migrate_memory.py](migrate_memory.md) | Migration runner for the m3-memory SQLite databases. |  |
| [bin/mission_control.py](mission_control.md) | mission_control.py — Cross-platform pulse dashboard (macOS / Windows / Linux). | yes |
| [bin/news_fetcher.py](news_fetcher.md) | (no docstring) |  |
| [bin/pg_setup.py](pg_setup.md) | (no docstring) |  |
| [bin/pg_sync.py](pg_sync.md) | (no docstring) |  |
| [bin/re_embed_all.py](re_embed_all.md) | (no docstring) |  |
| [bin/secret_rotator.py](secret_rotator.md) | (no docstring) |  |
| [bin/session_handoff.py](session_handoff.md) | (no docstring) |  |
| [bin/setup_secret.py](setup_secret.md) | Interactive CLI for adding API keys to the m3-memory encrypted vault. |  |
| [bin/setup_test_db.py](setup_test_db.md) | Seed a fresh SQLite DB with the full m3-memory schema for test isolation. |  |
| [bin/slm_intent.py](slm_intent.md) | Small-Language-Model intent classifier with named-profile configs. |  |
| [bin/sync_all.py](sync_all.md) | sync_all.py — Hourly sync runner (SQLite <-> PostgreSQL + ChromaDB). |  |
| [bin/temporal_utils.py](temporal_utils.md) | Enhanced temporal resolution utility for m3-memory. |  |
| [bin/test_bulk_parity.py](test_bulk_parity.md) | Real integration tests for memory_write_bulk_impl. |  |
| [bin/test_debug_agent.py](test_debug_agent.md) | End-to-end test suite for debug_agent_bridge.py. |  |
| [bin/test_keychain.py](test_keychain.md) | (no docstring) |  |
| [bin/test_knowledge.py](test_knowledge.md) | (no docstring) |  |
| [bin/test_mcp_proxy.py](test_mcp_proxy.md) | test_mcp_proxy.py — End-to-end proxy test suite |  |
| [bin/test_mcp_proxy_unit.py](test_mcp_proxy_unit.md) | test_mcp_proxy_unit.py - In-process unit tests for mcp_proxy. |  |
| [bin/test_memory_bridge.py](test_memory_bridge.md) | End-to-end test suite for memory_bridge.py. |  |
| [bin/test_unified_router.py](test_unified_router.md) | (no docstring) |  |
| [bin/web_research_bridge.py](web_research_bridge.md) | (no docstring) |  |
| [bin/weekly_auditor.py](weekly_auditor.md) | Weekly Audit Report -- M3 Memory |  |
| [install_os.py](install_os.md) | (no docstring) |  |
| [run_tests.py](run_tests.md) | (no docstring) |  |
| [scan_repo_v7.py](scan_repo_v7.md) | Scan orchestrator for the m3-memory security pipeline on LXC 504. |  |
| [scripts/inventory_graph.py](inventory_graph.md) | Build a mermaid call-graph from tool-inventory markdown files. |  |
| [scripts/metadata_filler.py](metadata_filler.md) | (no docstring) |  |
| [scripts/test_focus_fix.py](test_focus_fix.md) | (no docstring) |  |
| [validate_env.py](validate_env.md) | (no docstring) |  |
