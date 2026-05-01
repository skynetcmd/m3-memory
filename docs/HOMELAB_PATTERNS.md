# <a href="../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> M3 Memory — Homelab Patterns

> Last updated: May 2026. Practical patterns for running M3 in homelab and small-server environments. Corrections and contributions welcome via [issue](https://github.com/skynetcmd/m3-memory/issues).

This guide covers how to deploy M3 Memory in homelab setups — N100 boxes, mini-PCs, single-board computers, multi-LLM workstations, mixed-OS households — and the patterns we've seen work well there.

---

## 🏠 Why M3 fits homelabs

A few properties of M3's design happen to line up with what homelabs actually need:

- **Concurrency-safe by default.** SQLite WAL handles multiple agents writing simultaneously without races. If you're running Claude Code on a laptop, Gemini CLI on a workstation, and a couple of background agents reacting to Home Assistant events, they can all share one M3 store without coordination.
- **No GPU tax for memory operations.** Storage, retrieval, and graph traversal are all CPU + RAM only. Your GPU stays free for the LLM doing actual inference. Memory enrichment (the optional SLM-driven extraction layer) can run on the same GPU between calls, or on a separate small box, or be skipped entirely.
- **Offline-tolerant.** No external dependencies in the data path. Internet drops don't stop your agents. Useful at the cabin, on the road, or in environments where intermittent connectivity is the rule.
- **Cross-machine sync without a SaaS dependency.** Optional bi-directional delta sync via PostgreSQL or ChromaDB. One env var, your memories follow you across boxes.
- **Single-file SQLite.** Your entire memory store is one file. Easy to back up, easy to inspect with the standard `sqlite3` CLI, easy to move between machines.
- **Runs everywhere.** macOS, Linux, Windows — same `pip install`. No Docker requirement, no Kubernetes, no service mesh.

**What M3 doesn't try to be:** a high-scale multi-tenant SaaS. If you're building memory for thousands of paying users with team dashboards and SLAs, a hosted cognitive memory product (Mem0 cloud, Letta Cloud, Zep) probably fits better. M3 is built for one developer or one household running real agents on real hardware they own.

---

## 🧠 Picking your cognition placement

M3 ships with both a deterministic substrate and an SLM-driven extraction pipeline. Homelab users typically pick one of three patterns:

### Pattern A: M3 as raw substrate (no SLM extraction)

You write entities directly via MCP tools. Retrieval is pure SQLite + vector + graph traversal. No LLM in the memory data path.

**When this fits:**
- You already have a coding agent doing the thinking; you just want a place to put facts.
- You want maximum determinism (debugging memory issues without "the LLM extracted it differently this time").
- You're running on a small box (N100, Pi 5, mini-PC) where you'd rather not spend cycles on background extraction.
- You want the ability to swap the extraction layer (e.g., switch from a regex-based parser to an SLM later) without touching the storage layer.

**Setup:**
```bash
pip install m3-memory
# That's it. Don't run m3_enrich. Use mcp__memory__memory_write directly.
```

### Pattern B: M3 + built-in SLM extraction

M3's `m3_enrich` reads conversational turns and emits typed observations (facts, preferences, decisions). The reflector resolves contradictions via supersedes relationships.

**When this fits:**
- You have a GPU box (RTX 30/40/50 series, M-series Mac) and you're happy spending some of it on memory enrichment.
- You want auto-extracted facts without writing your own extraction prompt.
- You're running enough conversational volume that manual entity entry would be tedious.

**Setup:**
```bash
# 1. Install LM Studio, load qwen3-8b (or any compatible local SLM)
# 2. Start the LM Studio server (default port 1234)
# 3. Run enrichment over your conversation logs:
python bin/m3_enrich.py --profile enrich_local_qwen \
    --core --core-db memory/agent_memory.db \
    --source-variant chatlogs \
    --target-variant observations \
    --concurrency 4
```

Other profiles: `enrich_anthropic_haiku.yaml` (cloud, ~$3/1000 conversations), `enrich_google_gemini.yaml` (cloud, cheapest), `enrich_local_gemma.yaml` (faster local, less synthesis ability).

### Pattern C: M3 substrate + your own lightweight extraction layer

You bypass `m3_enrich` and run your own minimal extraction pipeline upstream of M3. Useful when you want extraction policy fully under your control, or when the M3 SLM pipeline is more than you need.

A good homelab-friendly recipe:

1. **Slugify entity names** for stable IDs (`"John Adams"` → `john-adams`).
2. **Maintain an alias table** for known synonyms (`"NYC"` → `new-york-city`, `"MSFT"` → `microsoft`). A small JSON file or a dedicated namespace in M3.
3. **Run a single LLM call** to extract entities from new text — return JSON with people / places / organizations / dates / events.
4. **Run a second small LLM call** for coreference resolution against the entities already in your context window.
5. **Write the resolved entities and relationships** into M3 via the standard MCP tools.

This is "Hindsight-lite": you get most of the cognitive benefits at a fraction of the compute cost, and you own every step of the pipeline.

---

## 🔌 Hardware sizing notes

Rough guidance from real deployments. Adjust to your workload:

| Box | What works | What to watch out for |
|---|---|---|
| **Raspberry Pi 5 / N100 mini-PC** | M3 substrate (Pattern A), ~10K-50K memories, occasional MCP queries from a single agent | Don't run SLM enrichment on the same box; offload to a GPU machine |
| **N5 Pro / Minisforum / 8-core mini** | M3 substrate + light enrichment via small SLM (gemma-2-2b, qwen-3b) | CPU inference is slow; batch enrichment overnight rather than realtime |
| **Workstation w/ 12GB+ GPU** | Full Pattern B with qwen3-8b extraction in realtime | Watch VRAM contention if the same GPU is also running your main agent |
| **Apple Silicon (M-series)** | All patterns; Metal-accelerated inference makes Pattern B cheap | LM Studio + qwen3-8b runs comfortably alongside Claude Code |
| **Mixed multi-box homelab** | Run M3 store on a small always-on box; run enrichment on the GPU box; sync if you want laptop access | Decide early which box is the "source of truth" — the SQLite WAL file should live there |

The store itself is small. Even with 100K memories and embeddings, the `agent_memory.db` is typically a few hundred MB to ~2 GB depending on embedding dimensions. Disk is rarely the constraint; RAM during enrichment is.

---

## 🤝 Multi-agent in a homelab

This is where M3 earns its keep. A typical pattern:

- **One M3 store** (single SQLite file on whichever box is most always-on).
- **Multiple agents** read and write through MCP — a Claude Code instance on your laptop, a Gemini CLI agent on your desktop, an OpenCode agent on a server, plus any background workers (a Home Assistant integration, a periodic web scraper, etc.).
- **Per-agent scoping** via `agent_id` and optional `scope` keeps each agent's working memory tidy without preventing cross-agent reads. Use the agent registry (`mcp__memory__agent_register`) so each writer is identified.
- **Handoffs** via `memory_handoff` — agent A leaves a structured task for agent B, agent B's next session sees it via its inbox.
- **Notifications** — for cross-machine signaling without a separate message bus.

You don't need a coordinator process. The semantics are:
- Writes are atomic (SQLite WAL).
- Contradictions auto-resolve via supersedes relationships.
- Agents see each other's writes immediately.

If you've ever tried to coordinate multiple agents over a Redis pub/sub or a shared filesystem, this is dramatically simpler.

---

## 🛡️ Resilience patterns

Things we've seen go wrong, and what to do about them:

- **Backups.** The `agent_memory.db` is a single file. `cp` it during a quiet moment, or use `sqlite3 .backup` for an online snapshot. Restic / Borg / your existing backup tool is fine — it's just a file.
- **Power loss.** SQLite WAL is crash-safe by default. After a hard reboot, the database recovers cleanly on the next open. No fsck-style intervention needed.
- **Multi-machine sync conflicts.** The bi-directional delta sync is last-writer-wins per memory ID, with conflict logs preserved in a separate table. If two machines edited the same memory while disconnected, the later edit wins but you can audit and replay.
- **Disk full.** SQLite handles disk-full gracefully — writes fail, reads keep working. Configure retention (`memory_set_retention`) and decay if your store grows faster than expected.
- **Bad model output during enrichment.** Pin model temperature in the YAML profile **and** verify it server-side. LM Studio's UI temperature can silently override profile settings when the model is reloaded — at temp=0.8 you'll get non-deterministic extraction even when the profile says temp=0. If results vary unexpectedly between supposedly-identical runs, suspect this before chasing prompt or model bugs.

---

## 🔗 Related docs

- [Multi-agent orchestration](MULTI_AGENT.md) — full multi-agent setup details
- [Configuration & environment variables](ENVIRONMENT_VARIABLES.md) — what to tune for small boxes
- [Architecture](ARCHITECTURE.md) — system design that makes the homelab patterns work
- [Comparison guide](COMPARISON.md) — when M3 is the right choice and when it isn't
- [Sovereign substrates table](https://html-preview.github.io/?url=https://github.com/skynetcmd/m3-memory/blob/main/docs/M3_Comparison_Table.html) — broader landscape view

---

## Contributing patterns

If you've got a homelab pattern that works for you and isn't covered here — multi-LLM-server setups, ESP32 sensor capture, Home Assistant + M3 integration, NAS-hosted memory with multiple thin clients — open an [issue](https://github.com/skynetcmd/m3-memory/issues) or PR. Real-world deployments are the most useful documentation.
