---
name: m3-agents
description: List registered agents and their last heartbeat. Useful for multi-agent setups.
---
# M3 Agents

## When to Use
Use this skill when you want to view all registered agents and their last heartbeat or check active participants in a multi-agent or mixed-fleet setup.

## Instructions
Call the `m3:agent_list` MCP tool.

Render each agent on one line: `agent_id (model) — last seen <relative time>, status=<online|offline|stale>`.

If only one agent is registered, that's normal for a single-machine install — multi-agent orchestration is opt-in.
