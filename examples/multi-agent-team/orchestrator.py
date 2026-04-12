"""
Multi-agent team orchestrator for m3-memory.

Polls the m3-memory notifications queue for each registered agent and
dispatches incoming tasks/handoffs to the right LLM provider. Provider-
agnostic — agents and providers are declared in team.yaml, not in code.

Run:
    python orchestrator.py team.yaml

Add a new agent: edit team.yaml.
Add a new provider with a new wire format: add an entry under `providers`
in team.yaml AND a branch in `dispatch()` below.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

# Resolve repo root so we can import bin/ modules without installing the package.
# bin/memory_core.py uses sibling imports (e.g. `from m3_sdk import ...`), so we
# put bin/ itself on sys.path rather than importing it as a package.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import memory_core  # noqa: E402
from agent_protocol import AgentProtocol  # noqa: E402

PROTOCOL = AgentProtocol()
HTTP = httpx.AsyncClient(timeout=120)


def load_team(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def register_team(team: dict) -> None:
    """Register every agent in team.yaml with m3-memory. Idempotent (UPSERT)."""
    for agent in team["agents"]:
        result = memory_core.agent_register_impl(
            agent_id=agent["name"],
            role=agent["role"],
            capabilities=agent.get("capabilities", []),
            metadata={"provider": agent["provider"], "model": agent["model"]},
        )
        print(f"  {result}")


async def call_provider(provider_cfg: dict, model: str, messages: list[dict]) -> str:
    """Send an OpenAI-style message list to a provider, return assistant text.

    Translation is handled by AgentProtocol so the rest of the orchestrator
    only ever sees OpenAI-shaped requests and responses.
    """
    fmt = provider_cfg["format"]
    api_key = os.getenv(provider_cfg["api_key_env"], "")

    if fmt == "openai_compat":
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        payload = {"model": model, "messages": messages}
        resp = await HTTP.post(provider_cfg["base_url"], json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    if fmt == "anthropic":
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        payload = PROTOCOL.openai_to_anthropic(messages, model)
        resp = await HTTP.post(provider_cfg["base_url"], json=payload, headers=headers)
        resp.raise_for_status()
        translated = PROTOCOL.translate_response(resp.json(), "anthropic")
        return translated["choices"][0]["message"]["content"]

    if fmt == "gemini":
        url = f"{provider_cfg['base_url']}/{model}:generateContent?key={api_key}"
        payload = PROTOCOL.openai_to_gemini(messages, model)
        resp = await HTTP.post(url, json=payload)
        resp.raise_for_status()
        translated = PROTOCOL.translate_response(resp.json(), "gemini")
        return translated["choices"][0]["message"]["content"]

    raise ValueError(f"Unknown provider format: {fmt!r}")


def build_messages(agent: dict, notification: dict, task_record: str | None) -> list[dict]:
    """Construct an OpenAI-style message list for one dispatch."""
    payload = json.loads(notification.get("payload_json") or "{}")
    kind = notification["kind"]

    user_lines = [
        f"You have a new {kind} notification.",
        f"Notification payload: {json.dumps(payload, indent=2)}",
    ]
    if task_record:
        user_lines += ["", "Task details from m3-memory:", task_record]
    user_lines += [
        "",
        "Use your m3-memory tools to read any context memories the planner",
        "attached, do the work, write the result to memory, and update the",
        "task state to 'completed' when you're done.",
    ]

    return [
        {"role": "system", "content": agent["system_prompt"]},
        {"role": "user", "content": "\n".join(user_lines)},
    ]


def fetch_unread(agent_id: str, kinds: list[str]) -> list[dict]:
    """Pull unread notifications for one agent, filtered by kind."""
    with memory_core._db() as db:
        placeholders = ",".join("?" * len(kinds))
        rows = db.execute(
            f"SELECT id, agent_id, kind, payload_json, created_at "
            f"FROM notifications "
            f"WHERE agent_id = ? AND read_at IS NULL AND kind IN ({placeholders}) "
            f"ORDER BY created_at ASC",
            (agent_id, *kinds),
        ).fetchall()
    return [dict(r) for r in rows]


async def handle_notification(agent: dict, providers: dict, notification: dict) -> None:
    """Dispatch one notification to its agent's provider and ack on success."""
    payload = json.loads(notification.get("payload_json") or "{}")
    task_id = payload.get("task_id")
    task_record = None
    if task_id:
        task_record = memory_core.task_get_impl(task_id)

    messages = build_messages(agent, notification, task_record)
    provider_cfg = providers[agent["provider"]]

    print(f"  -> dispatching to {agent['name']} ({agent['provider']}/{agent['model']})")
    try:
        reply = await call_provider(provider_cfg, agent["model"], messages)
    except Exception as e:
        print(f"  [FAIL] {agent['name']}: {e}")
        return

    # Persist the agent's reply as a memory the rest of the team can see.
    # memory_write_impl is async and returns a status string of the form
    # "Created: <uuid>" on success.
    result_status = await memory_core.memory_write_impl(
        content=reply,
        title=f"{agent['name']} reply: {notification['kind']}",
        type="note",
        agent_id=agent["name"],
        scope="agent",
    )
    print(f"  [OK] {agent['name']} replied: {result_status}")

    # If the notification was tied to a task, link the reply to the task.
    if task_id and isinstance(result_status, str) and "Created:" in result_status:
        new_id = result_status.split("Created:")[1].strip().split()[0]
        memory_core.task_set_result_impl(task_id, new_id)

    memory_core.notifications_ack_impl(notification["id"])


async def tick(team: dict) -> int:
    """One polling pass across all agents. Returns total dispatches handled."""
    providers = team["providers"]
    kinds = team["orchestrator"]["notification_kinds"]
    handled = 0

    for agent in team["agents"]:
        unread = fetch_unread(agent["name"], kinds)
        if not unread:
            continue
        print(f"[{agent['name']}] {len(unread)} notification(s)")
        for notification in unread:
            await handle_notification(agent, providers, notification)
            handled += 1

    return handled


async def main(team_path: Path) -> None:
    team = load_team(team_path)
    print(f"Loaded team from {team_path}")
    print("Registering agents with m3-memory:")
    register_team(team)

    interval = team["orchestrator"]["poll_interval_seconds"]
    max_iters = team["orchestrator"].get("max_iterations", 0)
    iteration = 0

    print(f"\nPolling every {interval}s. Ctrl-C to stop.\n")
    try:
        while True:
            iteration += 1
            handled = await tick(team)
            if handled == 0:
                print(f"[tick {iteration}] idle")
            if max_iters and iteration >= max_iters:
                break
            await asyncio.sleep(interval)
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        await HTTP.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-agent team orchestrator for m3-memory")
    parser.add_argument("team", nargs="?", default="team.yaml", help="path to team.yaml")
    args = parser.parse_args()
    asyncio.run(main(Path(args.team)))
