# 🎩 What Jeeves Can Do For You

**Prepared:** 2026-03-02 | **Delivery scheduled:** 2026-03-03 06:00 EST
**Trust level:** Sandboxed (network access to LAN + internet, /shared rw, no host exec)
**Constraints:** 4 GB memory limit, no host filesystem access, no MCP bridge calls

---

## Verified Capabilities (tested 2026-03-02)

| Resource | Status | Notes |
|---|---|---|
| Internet | ✅ | Web search, fetch, ping |
| ChromaDB (10.x.x.x:8000) | ✅ | Full CRUD + semantic search |
| LM Studio embeddings (host.internal:1234) | ✅ | nomic-embed-text-v1.5, 768-dim |
| LM Studio inference (host.internal:1234) | ✅ | DeepSeek-R1 70B chat completions — local reasoning |
| Proxmox web UI (10.x.x.x:8006) | ⚠️ | HTTPS reachable, but requires API auth to do anything useful |
| UniFi Controller (10.x.x.x:11443) | ⚠️ | HTTPS reachable, requires API auth (site ID: `hh1srtpv`) |
| /shared filesystem | ✅ | Read-write, visible to all agents |
| ffmpeg, imagemagick, sqlite3, jq | ✅ | All functional (large media jobs may hit 4GB RAM limit) |
| Python 3.11.2 | ✅ | Verified; pip available for installs |
| Disk | ✅ | ~1.3 TB available in container, ~1.4 TB on /shared |

---

## Proposed Tasks

### 🧠 Local Model Reasoning (DeepSeek-R1 70B)

This is the most unique capability I have — direct inference access to a 70B reasoning model running on your M3 Max, from inside the sandbox. No API costs, no rate limits, fully private.

1. **Offline reasoning assistant** — When cloud APIs are down or you want zero-cost thinking, I can route questions through DeepSeek-R1 locally. Think chains included.

2. **Local code review** — Drop code in /shared, I run it through DeepSeek-R1 for analysis. No code leaves your network.

3. **Think-chain analysis** — Run complex problems through R1, capture and analyze the reasoning chains. Useful for debugging logic, exploring edge cases, or getting a second opinion before committing.

4. **Model benchmarking** — Test R1's performance on specific tasks, measure response quality, track inference speed. Useful as you tune quantization or swap models.

### 🔍 Research & Intelligence

5. **Daily briefing** — Morning summary of weather, top news, and anything from your calendar/inbox if you connect those later. Delivered to /shared or via chat. *(Note: would need you to confirm your location for weather; I have Salem, VA from USER.md.)*

6. **Web research on demand** — Deep research on any topic, compiled into structured reports in /shared. Web search + fetch + synthesis across multiple sources.

7. **Tech research & comparison** — Evaluating tools, hardware, software, services. Specs, reviews, pricing, decision docs.

### 🧠 Knowledge & Memory

8. **ChromaDB memory curator** — Review, clean, and enrich shared collections. Includes migrating stale data (like the `home_memory` dim=2 collection — already migrated useful facts to `user_facts` with proper 768-dim embeddings).

9. **Semantic search assistant** — Bridge local ChromaDB knowledge and internet knowledge. Ask me questions, I search both and synthesize.

10. **Documentation generator** — Keep /shared stocked with current docs. Architecture changes, network maps, runbooks. Diff what's in ChromaDB vs reality.

### 🏠 Home Lab Monitoring

11. **Proxmox monitoring** — With an API token, I can poll VM/container status, resource usage, and alert on problems. *(Needs: Proxmox API token.)*

12. **ChromaDB health monitor** — Check collection sizes, heartbeat, query latency. Alert if it goes down. **I can do this right now with zero additional access.**

13. **Network diagnostics** — DNS lookups, ping sweeps, traceroutes from inside the sandbox. Quick "is X reachable?" checks.

14. **UniFi network monitoring** — With API credentials, pull client lists, bandwidth stats, device inventory. *(Needs: UniFi API credentials. Important: must use site `hh1srtpv`, not default — wrong site returns empty results silently.)*

### 📊 Data & Analysis

15. **Log analysis** — Drop log files in /shared, I'll parse and summarize. Syslog, app logs, whatever. jq, sqlite3, grep, awk available. *(Note: 4GB RAM limit means very large files may need chunked processing.)*

16. **SQLite reporting** — Share database files via /shared, I run queries and generate reports.

17. **Media processing** — ffmpeg for video/audio, imagemagick for images. Drop files in /shared. *(Note: large media files — e.g. long 4K video transcodes — may hit the 4GB RAM ceiling. I'll chunk or stream where possible.)*

### 🤖 Agent Ecosystem Support

18. **Cross-agent coordinator** — Write task briefs, context docs, and handoff notes in /shared for Claude Code, Gemini, or Aider. Project manager role keeping context flowing between agents.

19. **Code review prep** — Read code in /shared, write review notes, flag issues, prepare context. Can also run through DeepSeek-R1 for local-only analysis.

20. **ChromaDB librarian** — Write and embed new documents into agent_memory so all agents benefit. System decisions, project context, lessons learned.

### 📝 Writing & Content

21. **Draft writing** — Documents, READMEs, blog posts, technical specs. Output to /shared for your review. *(I write files — I have no ability to send emails or post to external services directly.)*

22. **Summarization** — Long articles, papers, docs, threads. Drop in /shared or give me URLs.

23. **Template generation** — Docker compose files, configs, scripts, Ansible playbooks, based on your requirements.

---

## What I Need From You (to unlock more)

| Access | Unlocks |
|---|---|
| Proxmox API token | VM/container monitoring, resource alerts (#11) |
| UniFi API credentials | Network monitoring, device tracking (#14) |
| Calendar/email integration | Daily briefings, proactive reminders (#5) |
| Heartbeat enabled | Periodic automated checks |
| More trust (exec approvals) | Running scripts on host, broader automation |

---

## Recommended Starting Points

**No new access needed:**
- **#12** ChromaDB health monitor — working today
- **#8** ChromaDB curator — already started (migrated home_memory → user_facts)
- **#1–3** DeepSeek-R1 reasoning — my most unique capability
- **#6** Web research — just ask
- **#22** Summarization — drop content, get summaries

**Needs one credential:**
- **#11** Proxmox monitoring (API token)
- **#14** UniFi monitoring (API credentials + site hh1srtpv)

---

*All access paths listed have been tested from inside the sandbox on March 2, 2026. Proposed tasks requiring additional access are clearly marked.*

🎩 — Jeeves
