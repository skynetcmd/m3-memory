# <a href="./README.md"><img src="docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> Multi-Agent Orchestration

M3 Memory provides the persistent memory and coordination substrate for multi-agent workflows. Agents share knowledge through scoped memory, pass context through handoffs and inboxes, and coordinate work through tasks, notifications, and a recursive task tree.

M3 Memory is not an agent runtime — it does not schedule or execute agents. It is the memory layer underneath your orchestrator, whether that is the bundled `m3-team` CLI, a LangGraph pipeline, or your own polling loop.

## Primitives

### Agent registry

Every agent registers with an ID and description. The registry tracks liveness via heartbeats and provides a directory other agents can query.

| Tool | Purpose |
|---|---|
| `agent_register` | Register an agent with ID, description, and capabilities |
| `agent_heartbeat` | Signal liveness |
| `agent_list` | List registered agents and their status |
| `agent_get` | Get details for a specific agent |
| `agent_offline` | Mark an agent offline |

### Scoped memory

Memory is isolated by `scope` so agents can maintain private working notes while sharing project-level knowledge.

- **`agent`** (default) — private to the writing agent. Use for implementation notes, scratch work, and internal reasoning.
- **`org`** — shared across all agents. Use for requirements, decisions, contracts, and any fact the whole team should see.
- **`user`** — scoped to a specific user/data subject. Use for user preferences, profile data, and GDPR-relevant records.

All scopes support the same `memory_search`, `memory_write`, and `memory_update` operations. Scope filtering is applied at query time — an agent searching `scope="org"` sees only org-scoped memories.

### Handoffs and inbox

When one agent finishes and the next needs to pick up, use `memory_handoff` to push context directly into the receiving agent's inbox. The receiver calls `memory_inbox` to read pending items and `memory_inbox_ack` to clear them.

```python
# Planner hands off to implementer
await tool(session, "memory_handoff", {
    "from_agent_id": "planner",
    "to_agent_id": "implementer",
    "title": "Implementation context",
    "content": "Contract is GET /health → 200 OK. Keep it dependency-free.",
    "conversation_id": "release-2026-04"
})

# Implementer reads inbox
await tool(session, "memory_inbox", {"agent_id": "implementer"})
```

### Tasks

Tasks are first-class objects with ownership, status, priority, and results. A planner creates tasks, assigns them to workers, and workers set results when done.

| Tool | Purpose |
|---|---|
| `task_create` | Create a task with title, description, priority |
| `task_assign` | Assign a task to an agent (triggers a `task_assigned` notification) |
| `task_update` | Update status, description, or priority |
| `task_set_result` | Record the output of a completed task |
| `task_get` | Read a single task |
| `task_list` | List tasks, optionally filtered by agent or status |
| `task_tree` | Render a recursive subtree rooted at a parent task |
| `task_delete` | Soft-delete a task |

### Notifications

Agents discover work through a poll-based notification queue. The orchestrator polls each agent's queue and dispatches work when notifications arrive.

| Tool | Purpose |
|---|---|
| `notify` | Send a notification to an agent |
| `notifications_poll` | Read pending notifications for an agent |
| `notifications_ack` | Acknowledge a single notification |
| `notifications_ack_all` | Acknowledge all pending notifications |

### Conversation grouping

Tag related memories with a shared `conversation_id` to create a logical session boundary. This works across agents — a planner and implementer can both write to `conversation_id="release-2026-04"` and later search within that scope.

### Bitemporal queries

Every memory records when it was created and when it was valid. The `as_of` parameter on `memory_search` enables time-travel queries — useful for debugging past decisions or reconciling conflicting reports across agents.

## Workflow patterns

### Turn-based (sequential handoff)

One agent finishes, then passes context to the next. Each agent reads the inbox, searches shared memory, does its work, and hands off to the successor.

```
Planner → (handoff) → Implementer → (handoff) → Reviewer
```

1. Planner writes requirements to `scope="org"`, creates a task, assigns it, and hands off context.
2. Implementer reads inbox, searches shared memory, writes private implementation notes to `scope="agent"`, and sets the task result.
3. Reviewer searches shared memory, verifies against the contract, and records approval.

### Parallel (fan-out / fan-in)

Multiple agents work simultaneously on independent tasks, reading from the same shared memory.

```
Planner → assigns Task A to Agent 1
        → assigns Task B to Agent 2
                                    → Reviewer merges results
```

Agents read `scope="org"` for shared context while keeping private notes in `scope="agent"`. The reviewer searches across both scopes to verify consistency.

### Hierarchical (task trees)

A parent task can have subtasks, forming a tree. Use `task_tree` to inspect the full hierarchy.

```python
parent = await tool(session, "task_create", {
    "agent_id": "planner",
    "title": "Ship release 2026-04",
    "priority": "high"
})

await tool(session, "task_create", {
    "agent_id": "planner",
    "title": "Implement /health endpoint",
    "parent_task_id": parent_id,
    "priority": "high"
})

await tool(session, "task_create", {
    "agent_id": "planner",
    "title": "Write documentation",
    "parent_task_id": parent_id,
    "priority": "medium"
})

# Inspect the full tree
await tool(session, "task_tree", {"root_task_id": parent_id})
```

### Blackboard (shared knowledge base)

All agents contribute facts and observations to `scope="org"` asynchronously, without a predefined sequence. Any agent can search the shared pool at any time. This is useful when agents are loosely coupled — each contributes what it knows, and others consume what they need.

### Reactive (notification-driven)

Agents react to events rather than following a fixed sequence. Add `task_completed` to the orchestrator's `notification_kinds` so a planner automatically wakes up when a subtask finishes. Agents can also use `notify` to broadcast custom signals.

## Full example

The [`examples/multi-agent-team/`](./examples/multi-agent-team/) directory contains a complete, runnable orchestrator:

- **`team.yaml`** — declarative agent definitions with provider, model, role, capabilities, and tool allowlists
- **`orchestrator.py`** — polling loop that discovers work via notifications and dispatches to agents
- **`dispatch.py`** — provider-agnostic multi-turn MCP dispatch with bounded execution (max turns, tool calls, wall clock, loop detection)

```bash
pip install -e .
m3-team init team.yaml
m3-team check
m3-team run
```

Any MCP client (Claude Code, Gemini CLI, etc.) can queue work:

```
task_create("Summarize the README", created_by="you")
task_assign(<task_id>, "local-agent")
```

The agent picks it up on the next tick, executes tool calls against m3-memory, and writes results back.

See [`examples/multi-agent-team/README.md`](./examples/multi-agent-team/README.md) for provider setup, resilience knobs, and how to add agents without code changes.

## Design principles

**Memory is the coordination layer.** Agents don't talk to each other directly — they read and write shared memory. This decouples agent execution from agent communication.

**Scope is the access control.** Private scratch work stays in `scope="agent"`. Shared decisions go to `scope="org"`. User data goes to `scope="user"` with GDPR primitives (`gdpr_export`, `gdpr_forget`) attached.

**The orchestrator is pluggable.** M3 Memory exposes primitives, not opinions about scheduling. The bundled `m3-team` is one orchestrator; you can build your own with the same tool catalog.
