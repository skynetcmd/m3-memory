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
