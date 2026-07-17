# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Memory — Roadmap

> Current version: **v2026.7.14.2** — actively maintained, with SOTA local-first retrieval (99.2% SHR@10, 100% @ k=20; 92% end-to-end QA, no oracle metadata on LongMemEval-S). Priorities shift based on community feedback; open an issue to vote on a feature.

---

## ✅ Shipped — 2026-Q2 highlights

Roughly two months of releases (≈25 between `v2026.4.12b` and `v2026.6.8.1`); grouped by theme below rather than version-by-version.

### Cross-platform install hardening (v2026.6.8.0 → v2026.6.8.1)

- [x] `install.sh` upgrades in place — no more "repo already exists" abort on re-run
- [x] Project Oxidation wheel installs into the pipx venv (not the sibling `repo/.venv` `mcp-memory` couldn't see)
- [x] 3-tier wheel install cascade — PyPI prebuilt → GitHub Release prebuilt → recommend source build
- [x] GitHub Release fallback is **required** for size-capped Linux CUDA (464 MB) and Windows CUDA (122 MB) wheels — both exceed PyPI's 100 MiB cap
- [x] `M3_EMBED_GGUF` auto-discovery + persistence — `~/.zshrc` / `~/.bashrc` on Unix, `setx` on Windows, plus the `memory` MCP server `env` block in `~/.claude/settings.json` and `~/.gemini/settings.json`
- [x] Build-tools preflight catches missing Rust toolchain — probes `~/.rustup/toolchains/<triple>/bin/cargo` in addition to PATH

### m3-core-rs 3.6.6 wheels (v2026.06.07)

- [x] 28 wheels: 7 (os × backend) combinations × cp311–cp314
- [x] PyPI distribution for 20 wheels (5 backends fit under the 100 MiB cap: `macos-metal`, `linux-cpu`, `linux-vulkan`, `windows-cpu`, `windows-vulkan`)
- [x] GitHub Release for all 28 — `linux-cuda` and `windows-cuda` stay GH-only by size policy
- [x] In-process Tier-1 embedder — Metal / CUDA / Vulkan / CPU backends
- [x] Sovereign Tier-2 HTTP embedder (`m3-embed-server`, BGE-M3 on port 8082) as launchd / systemd / Windows Service

### Retrieval quality + LongMemEval-S benchmarks (v2026.6.6.0, v2026.6.8.1)

- [x] **99.2% SHR @ k=10** on LongMemEval-S — full sweep **98.2% / 99.2% / 100.0%** @ k=5/10/20 (BGE-M3 hybrid FTS5 + vector + MMR; k=10 is M3's default search depth)
- [x] ~~89.0% E2E QA~~ (**superseded** — oracle-routed configuration; replaced by the 92.0% no-oracle figure below. Not a current or a recall number.)
- [x] **92.0% E2E QA — no oracle metadata** on LongMemEval-S (460 / 500, v3 inferred strategy routing, Claude Opus 4.6 answerer, gpt-4o judge) — supersedes the oracle-routed 89.0% headline; see the [LME-S Benchmarking Report](../benchmarks/longmemeval/LME-S_Benchmarking_Report.md) and [xiaowu0162/LongMemEval#49](https://github.com/xiaowu0162/LongMemEval/issues/49)
- [x] FTS5 sanitizer rewrite — allowlist tokenization fixes search crashes on queries containing hyphens, colons, `field:value` tokens (`gpt-4o`, `claude-code`, `100-200MB`, …)
- [x] LongMemEval-S benchmark harness shipped under `benchmarks/longmemeval/`
- [x] Live methodology + k-sweep + engine-upgrade addendum discussion: [xiaowu0162/LongMemEval#43](https://github.com/xiaowu0162/LongMemEval/issues/43)

### Files-memory subsystem (v2026.5.18.0 → v2026.5.29.5)

- [x] 26-tool files-memory layer — directory ingestion, hierarchical chunking, ascension to core memory, watch-mode staleness review
- [x] Files entity-linking + fact-extraction with bug fixes (v2026.5.29.4 / .5)
- [x] Modular `memory_core` refactor — submodule extraction with shim-preserved identity (no public surface drift; 322+ symbols snapshot-fingerprinted)

### Entity coalescing (v2026.5.29.7 → v2026.5.30.0)

- [x] Entity coalescing v1 → v2 — dedup/merge across mentions, structured `{a, b, score, title_a, title_b}` return per pair
- [x] MCP tool catalog registration for entity ops; CLI exit-code fix

### Engine v3 — bitemporal + decoupled roots (v2026.6.1.0)

- [x] Polars-accelerated bitemporal history analytics (pure-Python fallback retained; Polars optional)
- [x] Doctor quick-repair mode — `m3 doctor --fix` with `--dry-run` preview
- [x] SDK oxidation — Rust-backed FFI shims for `sysinfo`, advisory file locking (`fs2`), atomic circuit breakers (PyO3)
- [x] Decoupled config/engine roots (`M3_CONFIG_ROOT` / `M3_ENGINE_ROOT`) for clean security boundaries

### Multi-agent reach

- [x] **Hermes Agent** memory-provider plugin (file-based, vendored under `m3_memory/integrations/hermes/`)
- [x] **Antigravity** CLI/Desktop wiring via `~/.gemini/antigravity-cli/settings.json`
- [x] **OpenClaw** local proxy on `localhost:9000/v1` (no native MCP — bridged)
- [x] **LangChain / LangGraph** native surfaces — drop-in Mem0 replacement, LangMem-compatible `M3Store`, `M3Saver` LangGraph checkpointer (pause/resume/time-travel), chat-message history, RAG retriever, and LCEL-native `MemoryWrite`/`MemoryRetrieve`/`with_m3_memory` (`m3_memory/integrations/langchain/`, `pip install m3-memory[langchain]`)
- [x] Chatlog capture hooks — Claude Code (Stop + PreCompact, zero-gap) and Gemini CLI (SessionEnd)
- [x] curate-memory + curate-chatlog subagents with UUID-integrity defense (v2026.6.8.0)

### Knowledge maintenance — confidence, trust, reinforcement, beliefs

Memory as a maintained body of knowledge, not a flat index. All additive and
**off by default** (migrations 035–036). See `docs/CONFIDENCE_AND_TRUST.md`.

- [x] First-class **confidence** on `memory_items` (provenance + corroboration aggregate; NULL ⇒ importance)
- [x] **Trust-weighted / consensus provenance** — `agents.trust_score`, append-only corroboration ledger, `agent_set_trust` tool, corroboration-on-write (closes the orphan-duplicate gap)
- [x] **Reinforcement** — confidence decays toward NEUTRAL (uncertainty), re-aggregates from the ledger, access as weak capped evidence
- [x] **Autonomous episodic→semantic consolidation** — `belief` type, weekly `consolidate_beliefs.py`, reversible (soft-delete + edges)
- [x] **Confidence in ranking** behind `M3_CONFIDENCE_RANKING` (zero-regression flag-off contract, tested)

### Compliance + privacy

- [x] FIPS 140-3 deployment-ready: tiered crypto (`M3_FIPS_MODE` = hardened
  wolfCrypt, `M3_FIPS_STRICT` = CMVP-validated module), power-up KATs, secure
  DLL-hijack-resistant loading, `m3 fips install-wolfssl` helper, algorithm
  whitelist enforcement (see `FIPS_MODULE_BOUNDARY.md`)
- [x] GDPR Article 17 (`gdpr_forget`) + Article 20 (`gdpr_export`) as MCP tools
- [x] Per-conversation isolation enforced at the SQL layer (`WHERE conversation_id = ?` baked in)
- [x] Audit log entry via `_record_history` for every destructive op
- [x] Content-safety regex hardening (CodeQL #29, v2026.5.18.1)

### Windows hardening (v2026.5.29.3, .6)

- [x] Installer crash fixed
- [x] UTF-8 mode forced — eliminates the cp1252 emoji/box-drawing crash class
- [x] WMI-safe OS detection — no `platform.system()` hangs on Py3.14

### Sustained engineering

- [x] **100+ MCP tools** (was 66 at v2026.4.12b)
- [x] **1,283 tests across 154 files** (~2,070 cases with parametrization; was 193 at v2026.4.12b)
- [x] PyPI Trusted Publishing via OIDC — no token in CI
- [x] Pre-push tool-catalog drift gate + bench-data leakage scan (`.githooks/pre-push`)
- [x] CodeQL security gates + periodic Bandit + pip-audit + secrets-scan reports under [`docs/audits/`](./audits/)

---

## ✅ Shipped — Foundation (2026-Q1)

### v2026.4.12b — Conversation Grouping & Refresh Lifecycle (April 12)

- [x] `conversation_id` on `memory_write` / `memory_search` / `memory_update`
- [x] Refresh lifecycle — `refresh_on`, `refresh_reason`, `memory_refresh_queue`
- [x] Reversible migration system with backup/restore

### v2026.4.12 — Multi-Agent Orchestration (April 12)

- [x] Agent registry, handoffs, notifications, task trees
- [x] `m3-team` CLI for multi-agent teams from YAML
- [x] MCP proxy v2 — catalog-driven dispatch
- [x] License → Apache 2.0

### v2026.4.8 — PyPI Launch (April 10)

- [x] `pip install m3-memory` works out-of-the-box
- [x] `mcp-memory` CLI entry point auto-starts the server
- [x] `publish.yml` GitHub Actions — automated PyPI publish via OIDC

### v2026.04.06 — Initial Production Release (April 6)

- [x] Core memory system — write, search, update, delete, link
- [x] Hybrid retrieval — FTS5 + vector similarity + MMR re-ranking
- [x] Contradiction detection and bitemporal versioning
- [x] Knowledge graph (9 memory-link relationship types; entity-graph layer with a user-configurable 34-predicate vocabulary via `M3_ENTITY_VOCAB_YAML`)
- [x] Cross-device sync — SQLite ↔ PostgreSQL
- [x] LLM auto-classification, conversation summarization, memory consolidation

---

## 🚧 In progress

**Available now — stabilizing.** These subsystems are **shipped and usable today** (`pip install` includes them) but are still being hardened on real workloads before we feature them publicly in the README. Try them and please file issues.

- [x] **Web dashboard / observability portal** (`bin/dashboard_server.py`) — local FastAPI + HTMX UI: multi-DB overview, graph explorer, KB browser, conflict & audit log, background maintenance launcher. Run `python bin/dashboard_server.py` (listens on `127.0.0.1:8088`; override via `M3_DASHBOARD_HOST`/`M3_DASHBOARD_PORT`).
- [x] **Autonomous cognitive loop** (`bin/m3_cognitive_loop.py`) — background daemon running four enrichment stages: entity extraction, observation extraction, reflection (contradiction resolution), temporal resolution.
- [x] **Observer & Reflector SLM stages** (`bin/run_observer.py`, `bin/run_reflector.py`) — LLM-based semantic contradiction detection beyond embeddings.

- [ ] **LoCoMo audit** — harness scaffolded under `benchmarks/locomo/`; full run pending
- [ ] **Linux CUDA smoke-test automation** — wheels currently rely on manual verification on real NVIDIA hardware before each release
- [ ] **Per-project PyPI publish tokens** — replace the account-wide token used during the m3-core-rs 3.6.6 first-upload window
- [ ] **Description / metadata consistency** — keep `pyproject.toml`, GitHub repo description, README header tagline, and `docs/COMPARISON.md` claims in sync across releases

---

## 📦 Planned — Distribution & Deployment

- [ ] **pgvector / HNSW ANN on the PostgreSQL primary backend** — PostgreSQL can already be M3's primary live store (`M3_DB_BACKEND=postgres` + `M3_PRIMARY_PG_URL`), but vector search there is still **brute-force Rust cosine**, the same as on SQLite. Index-accelerated approximate nearest-neighbor via pgvector/HNSW is not yet implemented — so PG-primary today is about a shared/server store, not faster vector search.
- [ ] **Docker image** — `docker run -v ~/.m3-memory:/data ghcr.io/skynetcmd/m3-memory:latest`
- [ ] **Auto MCP Registry** — zero-config discovery in Claude Code and other MCP clients via published `mcp-server.json`
- [ ] **TestPyPI dry-run CI gate** — catch packaging regressions before every release
- [ ] **Homebrew formula** — `brew install m3-memory` (macOS / Linuxbrew)

---

## 📈 Planned — Observability & Web UI

> The **web dashboard** has shipped and is listed under [In progress / Available now](#-in-progress); the items below extend it.

- [ ] **Real-time contradiction log** — surfaced in dashboard and via `memory_verify` tool
- [ ] **Search explain mode** — show FTS5 score + vector score + MMR penalty breakdown for every result (today: `memory_suggest` returns these; UX still spartan)
- [ ] **Prometheus metrics endpoint** — latency, write/read counts, cache hit rates
- [ ] **Grafana dashboard template** — pre-built panels for memory health

---

## 👥 Planned — Multi-Agent & Collaboration

- [ ] **Shared memory namespaces** — optional scoped memory pools across multiple local agents
- [ ] **Agent identity model** — per-agent memory isolation with explicit cross-agent read grants (today: per-`agent_id` scoping at the SQL layer; the cross-agent grant primitive is still implicit)
- [ ] **Remote P2P sync** — encrypted memory replication over WireGuard / Tailscale without a central server
- [ ] **Memory access audit log** — who read/wrote what and when (GDPR Article 30 record)

---

## 🏆 Planned — Benchmark & Stability

- [ ] **Public benchmark suite** — MRR, Hit@5, latency vs. Mem0 / LangMem / raw ChromaDB on standard datasets (LongMemEval-S already published — see Shipped above)
- [ ] **Formal accuracy regression CI** — block merges that degrade retrieval quality (today: behavior baseline at `tests/capture_retrieval_baseline.py` is the local gate)
- [ ] **Stable public API** — `m3_memory.sdk` Python API with semver guarantees
- [ ] **Full documentation site** — MkDocs or Docusaurus with API reference, tutorials, architecture deep-dives
- [ ] **Plugin system** — register custom memory types, custom embedders, custom sync backends

---

## ❄️ Icebox (considering, no timeline)

- Browser extension for passive memory capture
- iOS / Android companion app for on-device sync
- LlamaIndex adapter (LangChain / LangGraph already shipped — see above)
- OpenTelemetry trace export

---

## 🤝 Contributing

Vote on features by reacting to [GitHub Issues](https://github.com/skynetcmd/m3-memory/issues) with 👍. Open a new issue with the `roadmap` label to propose something not listed here.

See [GOOD_FIRST_ISSUES.md](./GOOD_FIRST_ISSUES.md) for tasks ready to pick up right now.
