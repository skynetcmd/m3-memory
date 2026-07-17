# M3 Memory FAQ

## What is M3?

### Q: Is my data private?
**A:** Yes — 100% local. Memory lives in a SQLite file on your hardware, with zero cloud egress and zero telemetry. M3 runs fully air-gapped: the BGE-M3 embedder ships bundled and runs on your CPU with no API keys and no internet.

### Q: How good is retrieval?
**A:** State-of-the-art for a local-first substrate — **99.2% session-hit-rate @ k=10 and 100% @ k=20** on the LongMemEval-S benchmark (no oracle routing), with the correct session as the #1 result for ~92% of questions. End-to-end QA accuracy is 92.0% (no oracle metadata). See the [Benchmarking Report](../benchmarks/longmemeval/LME-S_Benchmarking_Report.md).

### Q: Can multiple agents share one memory?
**A:** Yes. Claude Code, Gemini CLI, Aider, OpenCode and any MCP agent share one brain, with optional SQL-layer isolation so each agent's private notes stay private. See [Multi-Agent](MULTI_AGENT.md).

### Q: Does it remember decisions across sessions?
**A:** Yes — that's the point. M3 is a bitemporal knowledge base: it captures facts, resolves contradictions automatically, and lets you query what your agent believed at any past date. A verbatim chatlog subsystem also records conversation turns *before* compaction, so nothing is lost to context-window truncation.

### Q: I need verbatim recall of facts — should I use a verbatim-only store instead?
**A:** No — M3 already gives you verbatim recall. Content is stored exactly as you wrote it and is **never altered in place**; the raw text is always retrievable byte-for-byte. When a fact is corrected, M3 doesn't overwrite the old one — it *closes* the old fact and links the new one, so the original wording stays queryable (via the `memory_history` tool, or an `as_of` point-in-time search) alongside the update. A verbatim-only store returns raw text too, but the moment a fact changes it loses the earlier version. M3 gives you exact recall **and** the full history of how a fact evolved — plus extraction and contradiction handling a plain verbatim store can't do.

### Q: Is M3 right for my project? When is a simpler approach better?
**A:** Be honest with yourself about the need. Persistent, evolving memory earns its keep when users (or agents) interact **repeatedly over time** and benefit from accumulated context — long-running autonomous agents, coding assistants that improve across sessions, personal/research assistants, multi-session workflows. If your need is really just **conversation history + RAG over a knowledge base + a small structured user profile**, that combination is simpler to build, test, and operate, and you may not need a memory framework at all. M3 doesn't punish you for starting small, though: you can run it as a plain store (disable enrichment/extraction — see below) and turn on the higher-order features only when you need them.

### Q: What should I check before adopting M3? (evaluation checklist)
**A:** Here are the standard "should I adopt this memory framework?" questions with M3's honest answers:

| Question | M3's answer |
|---|---|
| **Actively maintained?** | Yes — frequent releases (see [CHANGELOG](../CHANGELOG.md)). |
| **Memory format documented?** | Yes — a typed, code-cited schema ([MEMORY_MODEL.md](MEMORY_MODEL.md)) and a 100+ tool [API reference](API_REFERENCE.md). |
| **Swap the storage backend?** | **Partly — be aware:** SQLite is always the system of record. You can *sync/federate* to PostgreSQL ([SYNC.md](SYNC.md)), but you can't run M3 *on* Postgres as its live store. If a server-based store of record is a hard requirement, M3 isn't the fit. |
| **Customize what's remembered/forgotten?** | Yes — write-gating, importance, confidence decay, TTL/expiry, and per-agent retention policies ([MEMORY_MODEL.md](MEMORY_MODEL.md)). |
| **Debugging/introspection?** | Yes — `memory_suggest` returns a per-result score breakdown, `memory_history` shows the audit trail, `memory_verify` checks integrity, and `m3 doctor --fix` diagnoses the store. |
| **Integrates with my stack?** | Yes for **MCP** (native) and **LangChain/LangGraph** (drop-in — [LANGCHAIN.md](integrations/LANGCHAIN.md)). Beyond those, you call the MCP tools directly. |
| **Disable/replace components independently?** | Yes — run as a plain store (`M3_ENABLE_FACT_ENRICHED=0`, `M3_ENABLE_ENTITY_GRAPH=0`), swap the extractor (`M3_EXTRACTION_TYPE`), swap the embedder (`M3_EMBED_URL`/GGUF), or disable the Rust core — all via env vars, no fork ([ENVIRONMENT_VARIABLES.md](ENVIRONMENT_VARIABLES.md)). |
| **Portable data if I migrate away?** | Yes — `memory_export` / `gdpr_export` produce portable JSON (GDPR Article 20), and the store is a plain SQLite file any tool can read. |

---

## Windows Focus-Stealing Issues

### Q: Why do blank command prompt windows keep popping up and stealing focus?
**A:** Older installs registered the background scheduled tasks (like `AgentOS_ObservationDrain` or `AgentOS_ChatlogEmbedSweep`) to run through `cmd.exe`. `cmd.exe` is a console app, so Windows draws a window every time a task fires.

### Q: How do I fix it?
**A:** Run the fix script. It self-elevates — you can start it from a normal terminal and accept the UAC prompt:

```powershell
powershell -ExecutionPolicy Bypass -File bin\fix_scheduled_tasks.ps1
```

It re-registers every `AgentOS_*` task to run with `pythonw.exe` instead. `pythonw.exe` has no console subsystem, so the tasks run completely invisibly. The script prints a before/after summary so you can confirm the switch.

If you prefer to run it yourself in an **Administrator** terminal, the script just wraps this:
```powershell
python bin/install_schedules.py --repair
```

> The older "Hidden" trick (`Set-ScheduledTask ... -Hidden`) does **not** work for this — it only hides the task's row in the Task Scheduler UI, not the console window.

**macOS / Linux:** not affected — cron jobs never draw a window.

## General

### Q: Where are the logs located?
**A:** Logs are stored in the `logs/` directory at the project root.

### Q: My chat history is in the main memory DB instead of a separate chatlog DB. How do I split them?
**A:** This happens after switching from an integrated layout (chatlog sharing
the main DB) to separate files — repointing the path only routes new turns.
Move the existing rows with `bin/split_chatlog_from_core.py` (dry-run by default,
`--commit` to execute), then backfill embeddings (next question) since the move
only carries embeddings that already existed. Full steps, including repointing
the hooks so it sticks, are in
[docs/CHATLOG.md → Troubleshooting](CHATLOG.md#8-troubleshooting).

### Q: Chatlog search misses recent (or just-moved) turns. How do I backfill embeddings?
**A:** FTS5 keyword search works immediately, but vector/hybrid search needs the
rows embedded — a backlog builds up when the embed sweeper schedule isn't
installed, rows were bulk-imported, or you just moved rows between DBs. Check the
gap with `python bin/chatlog_status.py | grep without_embed`, then drain it:
`python bin/chatlog_embed_sweeper.py --deadline 0 --drain-spill`. The sweep is
idempotent (only embeds rows that lack one). Details in
[docs/CHATLOG.md → Backfilling missing embeddings](CHATLOG.md#backfilling-missing-embeddings).
