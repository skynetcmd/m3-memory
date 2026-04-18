# Tool inventory index

_Generated 2026-04-18T21:58:23.226275+00:00._

Re-run `python bin/gen_tool_inventory.py` after changing any tool.
Entries whose `sha1` no longer matches the live file need re-validation.

| Tool | Summary | Private |
|---|---|---|
| [bin/agent_protocol.py](agent_protocol.md) | (no docstring) |  |
| [bin/auth_utils.py](auth_utils.md) | (no docstring) |  |
| [bin/bench_locomo.py](bench_locomo.md) | Dialog-QA benchmark runner for m3-memory. |  |
| [bin/bench_longmemeval.py](bench_longmemeval.md) | Long-session QA benchmark runner for m3-memory. |  |
| [bin/bench_memory.py](bench_memory.md) | Memory system benchmark script. |  |
| [bin/chatlog_embed_sweeper.py](chatlog_embed_sweeper.md) | chatlog_embed_sweeper.py — lazy embed chat log rows missing embeddings. |  |
| [bin/chatlog_ingest.py](chatlog_ingest.md) | chatlog_ingest.py — single-entry-point CLI for ingesting host-agent chat logs. |  |
| [bin/chatlog_init.py](chatlog_init.md) | chatlog_init.py — interactive setup CLI for the chat log subsystem. |  |
| [bin/chatlog_status.py](chatlog_status.md) | chatlog_status.py — single-call summary of the chat log subsystem state. |  |
| [bin/chatlog_status_line.py](chatlog_status_line.md) | chatlog_status_line.py — anomaly-only status line generator. |  |
| [bin/chroma_sync_cli.py](chroma_sync_cli.md) | CLI wrapper for ChromaDB bi-directional sync. |  |
| [bin/cli_kb_browse.py](cli_kb_browse.md) | cli_kb_browse.py — Browse knowledge base entries in rank (importance) order. |  |
| [bin/cli_knowledge.py](cli_knowledge.md) | (no docstring) |  |
| [bin/custom_tool_bridge.py](custom_tool_bridge.md) | (no docstring) |  |
| [bin/debug_agent_bridge.py](debug_agent_bridge.md) | Debug Agent MCP Bridge — Autonomous debugging tools. |  |
| [bin/embed_agent_instructions.py](embed_agent_instructions.md) | One-shot script: embed AGENT_INSTRUCTIONS.md sections as searchable memory items. |  |
| [bin/embed_server.py](embed_server.md) | Local embedding server — OpenAI-compatible /v1/embeddings endpoint. |  |
| [bin/embed_server_gpu.py](embed_server_gpu.md) | AMD GPU Optimized Embedding Proxy — delegates to llama-server.exe. | yes |
| [bin/embedding_utils.py](embedding_utils.md) | Shared embedding and vector-math utilities for MCP bridges. |  |
| [bin/install_schedules.py](install_schedules.md) | M3 Memory: Cross-Platform Schedule Installer. |  |
| [bin/m3_sdk.py](m3_sdk.md) | (no docstring) |  |
| [bin/mcp_proxy.py](mcp_proxy.md) | MCP Tool Execution Proxy  v2.0 |  |
| [bin/mcp_tool_catalog.py](mcp_tool_catalog.md) | mcp_tool_catalog.py — single source of truth for the m3-memory MCP tool catalog. |  |
| [bin/memory_bridge.py](memory_bridge.md) | (no docstring) |  |
| [bin/memory_core.py](memory_core.md) | Core memory primitives: single + bulk write, search, enrichment, emitters. |  |
| [bin/memory_doctor.py](memory_doctor.md) | (no docstring) |  |
| [bin/migrate_flat_memory.py](migrate_flat_memory.md) | migrate_flat_memory.py — one-way ETL from flat-file / SQLite agent memory |  |
| [bin/migrate_memory.py](migrate_memory.md) | Migration runner for the m3-memory SQLite databases. |  |
| [bin/pg_setup.py](pg_setup.md) | (no docstring) |  |
| [bin/pg_sync.py](pg_sync.md) | (no docstring) |  |
| [bin/secret_rotator.py](secret_rotator.md) | (no docstring) |  |
| [bin/setup_secret.py](setup_secret.md) | Interactive CLI for adding API keys to the m3-memory encrypted vault. |  |
| [bin/sync_all.py](sync_all.md) | sync_all.py — Hourly sync runner (SQLite <-> PostgreSQL + ChromaDB). |  |
| [bin/temporal_utils.py](temporal_utils.md) | Enhanced temporal resolution utility for m3-memory. |  |
| [bin/test_bulk_parity.py](test_bulk_parity.md) | Real integration tests for memory_write_bulk_impl. |  |
| [bin/test_debug_agent.py](test_debug_agent.md) | End-to-end test suite for debug_agent_bridge.py. |  |
| [bin/test_mcp_proxy.py](test_mcp_proxy.md) | test_mcp_proxy.py — End-to-end proxy test suite |  |
| [bin/test_memory_bridge.py](test_memory_bridge.md) | End-to-end test suite for memory_bridge.py. |  |
| [bin/weekly_auditor.py](weekly_auditor.md) | Weekly Audit Report -- M3 Memory |  |
| [scripts/inventory_graph.py](inventory_graph.md) | Build a mermaid call-graph from tool-inventory markdown files. |  |
| [scripts/metadata_filler.py](metadata_filler.md) | (no docstring) |  |
