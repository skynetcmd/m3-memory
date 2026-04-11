# M3 Memory — Roadmap

> Status: **Production (v2026.4.7)** — active development. Priorities shift based on community feedback; open an issue to vote on a feature.

---

## v0.2 — Distribution & Deployment (Next)

- [ ] **Docker image** — `docker run -v ~/.m3-memory:/data ghcr.io/skynetcmd/m3-memory:latest`
- [ ] **Auto MCP Registry** — zero-config discovery in Claude Code and other MCP clients via published `mcp-server.json`
- [ ] **`pip install m3-memory` works out-of-the-box** — `mcp-memory` CLI entry point auto-starts the server
- [ ] **`setup.sh` / `install_os.py` polish** — OS-aware one-liner that validates deps and prints a ready-to-paste `mcp.json` snippet
- [ ] **TestPyPI dry-run CI gate** — catch packaging regressions before every release

---

## v0.3 — Observability & Web UI

- [ ] **Web dashboard** — lightweight local UI (FastAPI + HTMX) to browse memories, inspect knowledge graph, run GDPR operations
- [ ] **Real-time contradiction log** — surfaced in dashboard and via `memory_verify` tool
- [ ] **Search explain mode** — show FTS5 score + vector score + MMR penalty breakdown for every result
- [ ] **Prometheus metrics endpoint** — latency, write/read counts, cache hit rates
- [ ] **Grafana dashboard template** — pre-built panels for memory health

---

## v0.4 — Multi-Agent & Collaboration

- [ ] **Shared memory namespaces** — optional scoped memory pools across multiple local agents
- [ ] **Agent identity model** — per-agent memory isolation with explicit cross-agent read grants
- [ ] **Remote P2P sync** — encrypted memory replication over WireGuard / Tailscale without a central server
- [ ] **Memory access audit log** — who read/wrote what and when (GDPR Article 30 record)

---

## v1.0 — Benchmark Suite & Stability

- [ ] **Public benchmark suite** — MRR, Hit@5, latency vs. Mem0 / LangMem / raw ChromaDB on standard datasets
- [ ] **Formal accuracy regression CI** — block merges that degrade retrieval quality
- [ ] **Stable public API** — `m3_memory.sdk` Python API with semver guarantees
- [ ] **Full documentation site** — MkDocs or Docusaurus with API reference, tutorials, architecture deep-dives
- [ ] **Plugin system** — register custom memory types, custom embedders, custom sync backends

---

## Icebox (considering, no timeline)

- Browser extension for passive memory capture
- iOS / Android companion app for on-device sync
- LangChain / LlamaIndex adapter
- OpenTelemetry trace export

---

## Contributing

Vote on features by reacting to [GitHub Issues](https://github.com/skynetcmd/m3-memory/issues) with 👍. Open a new issue with the `roadmap` label to propose something not listed here.

See [GOOD_FIRST_ISSUES.md](./GOOD_FIRST_ISSUES.md) for tasks ready to pick up right now.
