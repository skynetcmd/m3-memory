#!/usr/bin/env python3
"""
One-shot script: embed ARCHITECTURE.md sections as searchable memory items.

Splits the file into 9 semantic sections, writes each as type=document
with embed=True. Idempotent: soft-deletes any prior architecture items
(agent_id="system", source="architecture") before writing fresh ones.
"""

import asyncio
import json
import os
import sqlite3
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(BASE_DIR, "bin"))

from memory_bridge import memory_delete, memory_write

DB_PATH = os.path.join(BASE_DIR, "memory", "agent_memory.db")

# ── Section definitions ───────────────────────────────────────────────────────
# Each tuple: (title, content)
SECTIONS = [
    (
        "Primary Engine — DeepSeek-R1 spec",
        """\
Model ID: deepseek-r1-distill-llama-70b-mlx
Quantization: 5-bit affine bfloat16 | Context: 64k-128k tokens
Max output tokens: 32768 | Read timeout: 4800s (~80 min at 7.5 tok/s)
Served by: LM Studio on localhost:1234 (start: lms server start)
Think chain: emitted in reasoning_content field — archived automatically by Protocol #1.
Embedding model: text-embedding-nomic-embed-text-v1.5 — 768 dims, also served by LM Studio.""",
    ),
    (
        "MCP Bridges — server names, scripts, tools",
        """\
custom_pc_tool  | bin/custom_tool_bridge.py  | log_activity, update_focus, query_decisions, retire_focus, check_thermal_load, query_local_model, web_search, grok_ask
memory          | bin/memory_bridge.py        | memory_write, memory_search, memory_update, memory_delete, memory_get, conversation_start, conversation_append, conversation_messages, conversation_search, chroma_sync, memory_maintenance
grok_intel      | bin/grok_bridge.py          | Grok 3 — real-time X/Twitter data and fast reasoning
web_research    | bin/web_research_bridge.py  | Perplexity sonar-pro — live web search
debug_agent     | bin/debug_agent_bridge.py   | Autonomous RCA — debug_analyze, debug_bisect, debug_trace

Canonical local model call: use query_local_model in custom_pc_tool.
Registered in: ~/.claude/settings.json and ~/.gemini/settings.json""",
    ),
    (
        "Memory System — tables, embedding, ChromaDB, maintenance",
        """\
DB: memory/agent_memory.db (SQLite)
Legacy tables: activity_logs, project_decisions, hardware_specs, system_focus
Memory tables: memory_items, memory_embeddings, memory_relationships, chroma_sync_queue

Embedding model: text-embedding-nomic-embed-text-v1.5 — 768 dims via LM Studio localhost:1234
Vector search: numpy batch cosine similarity; pure-Python fallback if numpy absent
Federation: ChromaDB at http://10.x.x.x:8000 (v2 API — v1 deprecated), collection agent_memory
  Collection live: 768-dim embeddings, synced via chroma_sync
  Offline-tolerant: writes queue to chroma_sync_queue; call chroma_sync to flush
  Other collections on same server: user_facts (768-dim), entities, home_memory
Maintenance: memory_maintenance — 0.995x importance decay per day after 7 days,
  purge items past expires_at, prune orphan embeddings
Phase 6 (Core Data): deferred — not implemented
Test suite: python3 bin/test_memory_bridge.py (48 tests, all pass)""",
    ),
    (
        "Protocol #1 — The Reasoning Rule",
        """\
PROTOCOL #1 — THE REASONING RULE

Trigger: query_local_model returns a response with non-empty reasoning_content (>200 chars).

Action: AUTOMATIC — the think chain is archived to activity_logs inside query_local_model.
No manual call needed for DeepSeek-R1 reasoning chains.

For manual complex reasoning (multi-step plans, architectural decisions):
  custom_pc_tool -> log_activity(
      category = "thought",
      detail_a = <topic or prompt, up to 500 chars>,
      detail_b = <reasoning summary, up to 2000 chars>
  )""",
    ),
    (
        "Protocol #2 — The Hardware Rule",
        """\
PROTOCOL #2 — THE HARDWARE RULE

Trigger: Suspected RAM pressure, thermal throttling, or after any heavy inference task.

Action (two-step):
1. custom_pc_tool -> check_thermal_load()
   Returns: Nominal | Fair | Serious | Critical

2. If status != Nominal, immediately call:
   custom_pc_tool -> log_activity(
       category = "hardware",
       detail_a = "thermal_pressure",
       detail_b = <status returned by check_thermal_load>
   )""",
    ),
    (
        "Protocol #3 — The Decision Rule",
        """\
PROTOCOL #3 — THE DECISION RULE

Trigger: User agrees to ANY code change, file move, diagnosis finding, or project direction change.

Action: Call IMMEDIATELY — do NOT wait until end of session, do NOT batch:
  custom_pc_tool -> log_activity(
      category = "decision",
      detail_a = <file, component, or area affected>,
      detail_b = <what was decided and why>,
      detail_c = <root cause or rationale>
  )

One call per decision at the time of agreement.""",
    ),
    (
        "Protocol #4 — The Search Rule",
        """\
PROTOCOL #4 — THE SEARCH RULE

Trigger: Before starting ANY new task.

Action: Call FIRST, before writing any code or plan:
  custom_pc_tool -> query_decisions(
      keyword = <topic keywords for the new task>,
      limit   = 10
  )

Review results for prior decisions, conflicts, or relevant context before proceeding.
This is the primary tool for institutional memory lookup.""",
    ),
    (
        "Protocol #5 — The Focus Protocol",
        """\
PROTOCOL #5 — THE FOCUS PROTOCOL

Trigger: Every 3 turns of a technical conversation.

Action:
  custom_pc_tool -> update_focus(
      summary = "<10-word summary of current trajectory>"
  )

When a task completes:
  custom_pc_tool -> retire_focus()

This keeps the Pulse dashboard current and provides a session breadcrumb trail.""",
    ),
    (
        "Auth model, bridge hardening standards, health checks",
        """\
AUTH MODEL — macOS Keychain 4-step resolution (token values never logged):
1. Environment variable matching the service name
2. Alternate env var (LM Studio only: LM_STUDIO_API_KEY)
3. macOS Keychain — primary service name
4. macOS Keychain — alternate service name

Keychain service names:
  LM Studio:  LM_STUDIO_API_KEY or LM_API_TOKEN
  Perplexity: PERPLEXITY_API_KEY
  Grok/xAI:   XAI_API_KEY

BRIDGE HARDENING STANDARDS (canonical reference: bin/custom_tool_bridge.py):
- All logging to stream=sys.stderr — stdout is MCP stdio transport, must stay clean
- Token values never written to logs
- Module-level constants for URLs, model IDs, MAX_TOKENS, READ_TIMEOUT
- Granular httpx.Timeout(connect=5.0, read=READ_TIMEOUT, write=10.0, pool=5.0)
- Typed exception handlers: specific -> DatabaseError -> Exception (no bare except)
- Catch-all returns type(exc).__name__ only — no str(e) to callers
- DB connections closed in finally blocks
- subprocess.CalledProcessError caught explicitly

HEALTH CHECKS:
  bash bin/mcp_check.sh                    — verify all 4 external endpoints
  python3 bin/test_memory_bridge.py        — full memory system (48 tests)

HOMELAB:
  ChromaDB:   http://10.x.x.x:8000 (Proxmox VMID 501)
  UniFi API:  https://10.x.x.x:11443/proxy/network/api/s/hh1srtpv/ (always site hh1srtpv)
  SSH:        UXG <UXG_USER>@<UXG_IP> | Controller root@<CONTROLLER_IP> | pve-database-host root@<PVE_IP> — see OS keyring for actual values""",
    ),
    (
        "OpenClaw Sandbox — container, volumes, networking, shell functions",
        """\
OPENCLAW SANDBOX

Container: openclaw-sandbox (node:22-slim, OrbStack/Docker)
Config: sandbox-openclaw/docker-compose.yml
Memory limit: 4 GB | Port: localhost:8000 -> container 18789

Volumes:
  /shared (rw) -> sandbox-openclaw/shared/ — drop zone for user and all agents
  /shared/ARCHITECTURE.md (ro) -> bind mount of ARCHITECTURE.md
  /home/clawuser/.openclaw (rw) -> sandbox-openclaw/.openclaw/

Runtime tools: curl, wget, ping, jq, ffmpeg, git-lfs, zip, unzip, sqlite3, dig/nslookup, pip3, file, tree, imagemagick

Shell functions: claw-grok, claw-claude, claw-gemini, claw-perplexity, claw-local (switch agent model)
  claw-pair (approve device pairing), de-claw (shutdown)

Dashboard: http://localhost:8000/?token=$OPENCLAW_GATEWAY_TOKEN
Doctor: docker exec openclaw-sandbox openclaw doctor

NETWORKING: OrbStack gives containers full LAN access — can reach ChromaDB at 10.x.x.x:8000
and all homelab VLANs. Container localhost is isolated from host localhost.""",
    ),
]

METADATA_BASE = json.dumps({
    "tags": ["architecture", "protocol", "system"],
    "source_file": "ARCHITECTURE.md",
})


async def main() -> None:
    print("=" * 60)
    print("  ARCHITECTURE.md — Embed as memory items")
    print("=" * 60)

    # ── Step 1: soft-delete any prior architecture items ──────────────────────
    print("\n[1/3] Cleaning prior architecture memory items...")
    conn = sqlite3.connect(DB_PATH)
    prior_ids = [
        r[0] for r in conn.execute(
            "SELECT id FROM memory_items WHERE agent_id = 'system' AND source = 'architecture'",
        ).fetchall()
    ]
    conn.close()

    if prior_ids:
        for pid in prior_ids:
            memory_delete(pid, hard=True)
        print(f"  Removed {len(prior_ids)} prior item(s).")
    else:
        print("  No prior items found — clean slate.")

    # ── Step 2: write + embed each section ───────────────────────────────────
    print(f"\n[2/3] Embedding {len(SECTIONS)} sections...")
    written_ids = []
    for i, (title, content) in enumerate(SECTIONS, 1):
        result = await memory_write(
            type="document",
            title=title,
            content=content,
            metadata=METADATA_BASE,
            agent_id="system",
            model_id="claude-sonnet-4-6",
            importance=0.9,
            source="architecture",
            embed=True,
        )
        item_id = result.replace("Created: ", "").strip()
        written_ids.append(item_id)
        print(f"  {i:2}. {title[:55]:<55} → {item_id[:8]}…")

    # ── Step 3: verify all embeddings landed ──────────────────────────────────
    print("\n[3/3] Verifying embeddings...")
    conn = sqlite3.connect(DB_PATH)
    ok = 0
    for item_id in written_ids:
        row = conn.execute(
            "SELECT dim FROM memory_embeddings WHERE memory_id = ?", (item_id,)
        ).fetchone()
        if row:
            ok += 1
            print(f"  ✅  {item_id[:8]}… dim={row[0]}")
        else:
            print(f"  ❌  {item_id[:8]}… NO EMBEDDING")
    conn.close()

    print(f"\n{'='*60}")
    print(f"  {ok}/{len(SECTIONS)} sections embedded successfully.")
    if ok == len(SECTIONS):
        print("  ARCHITECTURE.md is now fully searchable via memory_search.")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
