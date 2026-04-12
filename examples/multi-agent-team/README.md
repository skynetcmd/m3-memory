# Multi-agent team

A provider-agnostic orchestrator that runs a team of LLM agents on top of
m3-memory's orchestration primitives. Every agent is a config entry, not a
code change — add or remove agents by editing `team.yaml`.

## What it does

1. Reads `team.yaml`, registers every listed agent with `agent_register`.
2. Polls each agent's `notifications` queue on a tick.
3. For each unread `task_assigned` or `handoff` notification, dispatches a
   prompt to the agent's configured provider (Anthropic, OpenAI, Gemini,
   Grok, or any OpenAI-compatible endpoint).
4. Writes the reply back to m3-memory as a new memory and links it to the
   task via `task_set_result`.
5. Acks the notification so it isn't processed twice.

The orchestrator never knows or cares how many agents you have or which
providers they use — that's all in `team.yaml`.

## Run it

Set the API keys for the providers you actually use (only the ones
referenced in `team.yaml` need to exist):

```bash
export ANTHROPIC_API_KEY=...
export OPENAI_API_KEY=...
export GEMINI_API_KEY=...
export GROK_API_KEY=...
```

Install the two dependencies the orchestrator needs on top of m3-memory:

```bash
pip install httpx pyyaml
```

Then start it:

```bash
cd examples/multi-agent-team
python orchestrator.py team.yaml
```

In another terminal, drop work into the queue. The simplest path is to
use Claude Code (or any MCP client connected to m3-memory) and call:

```
task_create("Build feature X", created_by="you")
task_assign(<task_id>, "claude-planner")
```

The planner will pick it up on the next tick.

## Add an agent

Edit `team.yaml` and add a new entry under `agents`:

```yaml
- name: my-new-agent
  provider: openai          # must match a key in `providers`
  model: gpt-4.1-mini
  role: helper
  capabilities: [summarize, draft]
  system_prompt: |
    You are a fast helper for short summarization tasks.
```

Restart the orchestrator. The new agent is automatically registered with
m3-memory and starts receiving its share of notifications.

## Add a provider

If the new provider speaks the OpenAI chat completions format (Grok, Perplexity,
LM Studio, vLLM, LocalAI, OpenRouter, Together, etc.), just add a YAML block:

```yaml
providers:
  openrouter:
    format: openai_compat
    base_url: https://openrouter.ai/api/v1/chat/completions
    api_key_env: OPENROUTER_API_KEY
```

Then point an agent at it: `provider: openrouter`. No code changes.

If the provider has a wire format the orchestrator doesn't know yet, add
a new `format` value here and a matching branch in `call_provider()` in
`orchestrator.py`. The translation logic itself lives in
`bin/agent_protocol.py` — extend `AgentProtocol` if you need a brand-new
request/response shape.

## How it composes with m3-memory

The orchestrator only uses these m3-memory tools:

- `agent_register_impl` — at startup
- `notifications_*` — to discover and ack work
- `task_get_impl` — to read task details before dispatching
- `task_set_result_impl` — to link the agent's reply back to the task
- `memory_write_impl` — to persist the reply as a shared memory

Everything else (handoffs between agents, task state transitions, the
audit log in `memory_history`) is driven by the agents themselves through
their MCP tool calls. The orchestrator is just the wake signal.

## Knobs in team.yaml

```yaml
orchestrator:
  poll_interval_seconds: 5    # how often to check notifications
  max_iterations: 0           # 0 = forever; set N to stop after N ticks (handy for testing)
  notification_kinds:         # which kinds wake an agent
    - task_assigned
    - handoff
```

Add `task_completed` to `notification_kinds` if you want planners to react
to subtask completions automatically.
