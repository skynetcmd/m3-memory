---
name: agents
description: List registered agents and their last heartbeat. Useful for multi-agent setups.
---

Call `m3:agent_list`.

Render each agent on one line: `agent_id (model) — last seen <relative time>, status=<online|offline|stale>`.

If only one agent is registered, that's normal for a single-machine install — multi-agent orchestration is opt-in.
