# Tool inventory index

_Generated 2026-04-17T04:17:01.801620+00:00._

Re-run `python bin/gen_tool_inventory.py` after changing any tool.
Entries whose `sha1` no longer matches the live file need re-validation.

| Tool | Summary | Private |
|---|---|---|
| [bin/bench_locomo.py](bench_locomo.md) | Dialog-QA benchmark runner for m3-memory. |  |
| [bin/bench_longmemeval.py](bench_longmemeval.md) | Long-session QA benchmark runner for m3-memory. |  |
| [bin/bench_memory.py](bench_memory.md) | Memory system benchmark script. |  |
| [bin/chroma_sync_cli.py](chroma_sync_cli.md) | CLI wrapper for ChromaDB bi-directional sync. |  |
| [bin/cli_kb_browse.py](cli_kb_browse.md) | cli_kb_browse.py — Browse knowledge base entries in rank (importance) order. |  |
| [bin/cli_knowledge.py](cli_knowledge.md) | (no docstring) |  |
| [bin/embed_agent_instructions.py](embed_agent_instructions.md) | One-shot script: embed AGENT_INSTRUCTIONS.md sections as searchable memory items. |  |
| [bin/embed_server.py](embed_server.md) | Local embedding server — OpenAI-compatible /v1/embeddings endpoint. |  |
| [bin/embed_server_gpu.py](embed_server_gpu.md) | AMD GPU Optimized Embedding Proxy — delegates to llama-server.exe. | yes |
| [bin/install_schedules.py](install_schedules.md) | M3 Memory: Cross-Platform Schedule Installer. |  |
| [bin/memory_doctor.py](memory_doctor.md) | (no docstring) |  |
| [bin/migrate_flat_memory.py](migrate_flat_memory.md) | migrate_flat_memory.py — one-way ETL from flat-file / SQLite agent memory |  |
| [bin/migrate_memory.py](migrate_memory.md) | Migration runner for the m3-memory SQLite database. |  |
| [bin/pg_setup.py](pg_setup.md) | (no docstring) |  |
| [bin/pg_sync.py](pg_sync.md) | (no docstring) |  |
| [bin/secret_rotator.py](secret_rotator.md) | (no docstring) |  |
| [bin/setup_secret.py](setup_secret.md) | Interactive CLI for adding API keys to the m3-memory encrypted vault. |  |
| [bin/sync_all.py](sync_all.md) | sync_all.py — Hourly sync runner (SQLite <-> PostgreSQL + ChromaDB). |  |
| [bin/test_debug_agent.py](test_debug_agent.md) | End-to-end test suite for debug_agent_bridge.py. |  |
| [bin/test_mcp_proxy.py](test_mcp_proxy.md) | test_mcp_proxy.py — End-to-end proxy test suite |  |
| [bin/test_memory_bridge.py](test_memory_bridge.md) | End-to-end test suite for memory_bridge.py. |  |
| [bin/weekly_auditor.py](weekly_auditor.md) | Weekly Audit Report -- M3 Memory |  |
