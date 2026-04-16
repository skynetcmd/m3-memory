# <a href="./README.md"><img src="docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Memory — Roadmap

> Current version: **v2026.4.12b** — active development. Priorities shift based on community feedback; open an issue to vote on a feature.

---

## :white_check_mark: Shipped

### v2026.04.06 — Production Release (April 6, 2026)

- [x] Core memory system — write, search, update, delete, link
- [x] Hybrid retrieval — FTS5 + vector similarity + MMR re-ranking
- [x] Contradiction detection and bitemporal versioning
- [x] Knowledge graph with 8 relationship types
- [x] GDPR compliance — `gdpr_forget` (Article 17), `gdpr_export` (Article 20)
- [x] Cross-device sync — SQLite ↔ PostgreSQL ↔ ChromaDB
- [x] LLM auto-classification, conversation summarization, memory consolidation
- [x] 44 MCP tools, 41 end-to-end tests

### v2026.4.8 — PyPI Launch (April 10, 2026)

- [x] `pip install m3-memory` works out-of-the-box
- [x] `mcp-memory` CLI entry point auto-starts the server
- [x] `publish.yml` GitHub Actions — automated PyPI publish via OIDC
- [x] ROADMAP.md with community voting

### v2026.4.12 — Multi-Agent Orchestration (April 12, 2026)

- [x] Agent registry, handoffs, notifications, and task trees
- [x] `m3-team` CLI for multi-agent teams from YAML
- [x] MCP proxy v2 — catalog-driven dispatch, 46 tools
- [x] License → Apache 2.0

### v2026.4.12b — Conversation Grouping & Refresh Lifecycle (April 12, 2026)

- [x] `conversation_id` on memory_write / memory_search / memory_update
- [x] Refresh lifecycle — `refresh_on`, `refresh_reason`, `memory_refresh_queue`
- [x] Reversible migration system with backup/restore
- [x] 193 end-to-end tests

---

## :package: Next — Distribution & Deployment

- [ ] **Docker image** — `docker run -v ~/.m3-memory:/data ghcr.io/skynetcmd/m3-memory:latest`
- [ ] **Auto MCP Registry** — zero-config discovery in Claude Code and other MCP clients via published `mcp-server.json`
- [ ] **`setup.sh` / `install_os.py` polish** — OS-aware one-liner that validates deps and prints a ready-to-paste `mcp.json` snippet
- [ ] **TestPyPI dry-run CI gate** — catch packaging regressions before every release

---

## :chart_with_upwards_trend: Planned — Observability & Web UI

- [ ] **Web dashboard** — lightweight local UI (FastAPI + HTMX) to browse memories, inspect knowledge graph, run GDPR operations
- [ ] **Real-time contradiction log** — surfaced in dashboard and via `memory_verify` tool
- [ ] **Search explain mode** — show FTS5 score + vector score + MMR penalty breakdown for every result
- [ ] **Prometheus metrics endpoint** — latency, write/read counts, cache hit rates
- [ ] **Grafana dashboard template** — pre-built panels for memory health

---

## :busts_in_silhouette: Planned — Multi-Agent & Collaboration

- [ ] **Shared memory namespaces** — optional scoped memory pools across multiple local agents
- [ ] **Agent identity model** — per-agent memory isolation with explicit cross-agent read grants
- [ ] **Remote P2P sync** — encrypted memory replication over WireGuard / Tailscale without a central server
- [ ] **Memory access audit log** — who read/wrote what and when (GDPR Article 30 record)

---

## :trophy: Planned — Benchmark Suite & Stability

- [ ] **Public benchmark suite** — MRR, Hit@5, latency vs. Mem0 / LangMem / raw ChromaDB on standard datasets
- [ ] **Formal accuracy regression CI** — block merges that degrade retrieval quality
- [ ] **Stable public API** — `m3_memory.sdk` Python API with semver guarantees
- [ ] **Full documentation site** — MkDocs or Docusaurus with API reference, tutorials, architecture deep-dives
- [ ] **Plugin system** — register custom memory types, custom embedders, custom sync backends

---

## :snowflake: Icebox (considering, no timeline)

- Browser extension for passive memory capture
- iOS / Android companion app for on-device sync
- LangChain / LlamaIndex adapter
- OpenTelemetry trace export

---

## :handshake: Contributing

Vote on features by reacting to [GitHub Issues](https://github.com/skynetcmd/m3-memory/issues) with 👍. Open a new issue with the `roadmap` label to propose something not listed here.

See [GOOD_FIRST_ISSUES.md](./GOOD_FIRST_ISSUES.md) for tasks ready to pick up right now.
