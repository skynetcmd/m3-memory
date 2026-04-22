# <a href="../../README.md"><img src="https://raw.githubusercontent.com/skynetcmd/m3-memory/main/docs/icon.svg" height="60" style="vertical-align: baseline; margin-bottom: -15px;"></a> Multi-agent team

A provider-agnostic orchestrator that runs a team of LLM agents as **real
MCP clients** on top of m3-memory's orchestration primitives. Each agent
is a config entry, not a code change — add or remove agents by editing
`team.yaml`.

This is the v2 design. The orchestrator is a polling shell; every
notification triggers a multi-turn dispatch loop where the LLM can call
m3-memory tools mid-turn (read context, write results, mark tasks
complete). The orchestrator is no longer "just a wake signal" — it is a
full MCP host that brokers between the LLM and the in-process catalog of
44 m3-memory tools.

## What it does

1. Reads `team.yaml`, registers every listed agent with `agent_register`.
2. Polls each agent's `notifications` queue on a tick.
3. For each unread `task_assigned` or `handoff` notification, hands off
   to `dispatch.dispatch()`:
   - builds an OpenAI-shape `tools` list from that agent's allowlist,
   - calls the agent's provider with tool calling enabled,
   - executes any tool calls in-process through `mcp_tool_catalog`,
   - appends results to the message history,
   - loops until the model emits no more tool calls (or a bound is hit).
4. Acks the notification on terminal (except transient 5xx exhaustion,
   which is left on the queue for the next tick).
5. Prints a one-line summary per dispatch:
   `[agent] notif=N terminal=success turns=2 tool_calls=5 elapsed=1.2s`

The orchestrator never knows or cares how many agents you have or which
providers they use — that's all in `team.yaml`.

## Quickstart (zero API keys, local LM Studio)

```bash
pip install -e .
m3-team init team.yaml         # writes a starter file
# start LM Studio at http://localhost:1234, load any tool-calling model
m3-team check                  # validates yaml + flags missing env vars
m3-team run                    # starts polling
```

In another terminal, queue work via any MCP client connected to
m3-memory (Claude Code, Gemini CLI, etc.):

```
task_create("Summarize the README", created_by="you")
task_assign(<task_id>, "local-agent")
```

The local agent picks it up on the next tick, reads the file via
m3-memory tools, writes a summary back to memory, and marks the task
complete.

## Run with the full multi-provider team

Set the API keys for the providers you actually use (only the ones
referenced in `team.yaml` need to exist):

```bash
export ANTHROPIC_API_KEY=...
export OPENAI_API_KEY=...
export GEMINI_API_KEY=...
export GROK_API_KEY=...
```

Then either via the installed CLI:

```bash
m3-team run examples/multi-agent-team/team.yaml
```

…or directly:

```bash
cd examples/multi-agent-team
python orchestrator.py team.yaml
```

## Add an agent

Edit `team.yaml` and add a new entry under `agents`:

```yaml
- name: my-new-agent
  provider: openai          # must match a key in `providers`
  model: gpt-4.1-mini       # whatever the provider exposes today
  role: helper
  capabilities: [summarize, draft]
  tools:                    # optional — restrict what the agent can call
    allow: [memory_search, memory_get, memory_write, task_update]
  system_prompt: |
    You are a fast helper for short summarization tasks.
```

Restart the orchestrator. The new agent is automatically registered
with m3-memory and starts receiving its share of notifications.

If `tools.allow` is omitted, the agent gets the **catalog default
allowlist** — every m3-memory tool except destructive ones
(`memory_delete`, `gdpr_export`, `gdpr_forget`, `chroma_sync`,
`memory_maintenance`, `memory_set_retention`, `memory_export`,
`memory_import`, `agent_offline`). Use `tools.deny` to subtract
individual tools after `allow`.

## Add a provider

If the new provider speaks the OpenAI chat completions format (Grok,
Groq, Perplexity, OpenRouter, Together, LM Studio, vLLM, Ollama, etc.),
just add a YAML block:

```yaml
providers:
  openrouter:
    format: openai_compat
    base_url: https://openrouter.ai/api/v1/chat/completions
    api_key_env: OPENROUTER_API_KEY
```

Then point an agent at it: `provider: openrouter`. No code changes.

If the provider has a wire format the orchestrator doesn't know yet, add
a new `format` value here and a matching branch in
`dispatch._call_provider_with_retry()`. The translation logic itself
lives in `bin/agent_protocol.py` — extend `AgentProtocol` if you need a
brand-new request/response shape.

## How dispatch composes with m3-memory

The orchestrator and dispatch loop only use these m3-memory entry
points:

- `agent_register_impl` — at startup
- `notifications_*` — to discover and ack work
- `task_get_impl` — to read task details before dispatching
- `task_update_impl` — to mark a task failed on bounded-failure
  terminals
- `mcp_tool_catalog.execute_tool` — to run any tool the agent calls
  during a turn (this is the new piece — the LLM is a full MCP client,
  not a one-shot text producer)

Everything else (handoffs between agents, task state transitions, the
audit log in `memory_history`, `memory_write` results, allowlist
enforcement, agent_id injection) is driven by the agents themselves
through their MCP tool calls — but those calls are now executed
in-process by the orchestrator instead of being just suggestions in
returned text.

## Resilience knobs

Every dispatch runs under a strict per-call budget. Defaults live in
`team.yaml > orchestrator.dispatch_limits`:

```yaml
dispatch_limits:
  max_turns: 8                 # provider round-trips per dispatch
  max_tool_calls: 24           # total tool calls per dispatch
  max_seconds: 120             # wall clock per dispatch
  max_tokens_per_call: 4096    # provider max_tokens per request
  provider_retries: 3          # exponential backoff on 429 / 5xx
```

Plus three implicit guarantees that don't have knobs:

- **Loop detection**: 4 identical (tool_name, args) calls in a row
  trips a `loop_detected` terminal.
- **Provider retry**: 4xx is terminal immediately; 429, 5xx, Anthropic
  529, and Gemini `RESOURCE_EXHAUSTED` retry with exponential backoff
  (0.5s → 1s → 2s, capped at 4s).
- **Agent identity injection**: 6 tools (`memory_write`,
  `agent_heartbeat`, `agent_offline`, `memory_inbox`,
  `notifications_poll`, `notifications_ack_all`) have `agent_id`
  injected from the dispatch loop's known agent name. The LLM cannot
  spoof as another agent.

Bounded failures (`max_turns`, `tool_call_budget`, `timeout`,
`loop_detected`, `provider_error_4xx`) mark the task as `failed` with
the terminal name in metadata. Transient `provider_error_5xx_exhausted`
leaves the task untouched and the notification on the queue, so the
next tick retries.

## Knobs in team.yaml

```yaml
orchestrator:
  poll_interval_seconds: 5
  max_iterations: 0              # 0 = forever; N to stop after N idle ticks
  max_concurrent_dispatches: 4   # how many notifications run in parallel per tick
  notification_kinds:            # which kinds wake an agent
    - task_assigned
    - handoff
  dispatch_limits:
    # ...see above
```

Add `task_completed` to `notification_kinds` if you want planners to
react to subtask completions automatically.
```
