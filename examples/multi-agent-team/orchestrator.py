"""
Multi-agent team orchestrator for m3-memory (v2 — real MCP host).

The orchestrator is a thin polling shell. It does three things:

  1. Registers every agent in team.yaml with m3-memory at startup.
  2. On each tick, pulls unread notifications for every agent in parallel.
  3. Hands each notification to dispatch.dispatch(), which runs a real
     multi-turn MCP loop (the LLM can call m3-memory tools mid-turn).

Everything that has to do with prompting, tool calling, retries, loop
detection, and bounded failure handling lives in dispatch.py. The
orchestrator only sees a DispatchResult and decides whether to ack the
notification (terminal == success or any non-transient failure) or leave
it on the queue for next tick (transient 5xx exhaustion).

Run:
    python orchestrator.py team.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx
import yaml

# Resolve repo root so we can import bin/ modules without installing the package.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import memory_core  # noqa: E402

from dispatch import DispatchLimits, DispatchResult, dispatch  # noqa: E402

# Terminals that mean "do not retry this notification" — ack it.
# 5xx exhaustion is the only transient terminal: leave the notification on
# the queue so the next tick re-attempts.
TERMINAL_ACK = {
    "success",
    "max_turns",
    "tool_call_budget",
    "timeout",
    "loop_detected",
    "provider_error_4xx",
}


def load_team(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_dispatch_limits(team: dict) -> DispatchLimits:
    """Pull dispatch_limits out of team.yaml, falling back to defaults."""
    cfg = (team.get("orchestrator") or {}).get("dispatch_limits") or {}
    defaults = DispatchLimits()
    return DispatchLimits(
        max_turns=int(cfg.get("max_turns", defaults.max_turns)),
        max_tool_calls=int(cfg.get("max_tool_calls", defaults.max_tool_calls)),
        max_seconds=float(cfg.get("max_seconds", defaults.max_seconds)),
        max_tokens_per_call=int(cfg.get("max_tokens_per_call", defaults.max_tokens_per_call)),
        provider_retries=int(cfg.get("provider_retries", defaults.provider_retries)),
    )


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


def _format_terminal(agent_name: str, notif_id: int, result: DispatchResult) -> str:
    """One-line summary suitable for a single print() call."""
    head = (
        f"[{agent_name}] notif={notif_id} terminal={result.terminal} "
        f"turns={result.turns} tool_calls={result.total_tool_calls} "
        f"elapsed={result.elapsed_seconds:.1f}s"
    )
    if result.error:
        head += f" error={result.error[:160]}"
    return head


async def _run_one(agent: dict, provider_cfg: dict, notification: dict,
                   http: httpx.AsyncClient, limits: DispatchLimits) -> tuple[dict, DispatchResult]:
    """Look up the task record, dispatch, ack on terminal, return the result."""
    import json
    payload = json.loads(notification.get("payload_json") or "{}")
    task_id = payload.get("task_id")
    task_record = memory_core.task_get_impl(task_id) if task_id else None

    try:
        result = await dispatch(agent, provider_cfg, notification, task_record, http, limits)
    except Exception as e:
        result = DispatchResult(
            terminal="dispatch_exception",
            error=f"{type(e).__name__}: {e}",
        )

    if result.terminal in TERMINAL_ACK or result.terminal == "dispatch_exception":
        try:
            memory_core.notifications_ack_impl(notification["id"])
        except Exception as e:
            print(f"  [warn] ack failed for notif {notification['id']}: {e}")

    return notification, result


async def tick(team: dict, http: httpx.AsyncClient, limits: DispatchLimits,
               max_concurrent: int) -> int:
    """One polling pass. Returns total dispatches handled this tick."""
    providers = team["providers"]
    kinds = team["orchestrator"]["notification_kinds"]
    agents_by_name = {a["name"]: a for a in team["agents"]}

    jobs: list = []
    for agent in team["agents"]:
        for notification in fetch_unread(agent["name"], kinds):
            jobs.append((agent, notification))

    if not jobs:
        return 0

    sem = asyncio.Semaphore(max(1, max_concurrent))

    async def _bounded(agent, notification):
        async with sem:
            return await _run_one(
                agent, providers[agent["provider"]], notification, http, limits
            )

    results = await asyncio.gather(*(_bounded(a, n) for a, n in jobs))
    for notification, result in results:
        agent_name = notification["agent_id"]
        print("  " + _format_terminal(agent_name, notification["id"], result))
    return len(results)


async def main(team_path: Path) -> None:
    team = load_team(team_path)
    print(f"Loaded team from {team_path}")
    print("Registering agents with m3-memory:")
    register_team(team)

    orch = team["orchestrator"]
    interval = orch["poll_interval_seconds"]
    max_iters = orch.get("max_iterations", 0)
    max_concurrent = int(orch.get("max_concurrent_dispatches", 4))
    limits = build_dispatch_limits(team)

    print(
        f"\nDispatch limits: max_turns={limits.max_turns} "
        f"max_tool_calls={limits.max_tool_calls} "
        f"max_seconds={limits.max_seconds} "
        f"retries={limits.provider_retries}"
    )
    print(f"Polling every {interval}s, up to {max_concurrent} dispatches in parallel. Ctrl-C to stop.\n")

    iteration = 0
    async with httpx.AsyncClient(timeout=120) as http:
        try:
            while True:
                iteration += 1
                handled = await tick(team, http, limits, max_concurrent)
                if handled == 0:
                    print(f"[tick {iteration}] idle")
                    if max_iters and iteration >= max_iters:
                        break
                    await asyncio.sleep(interval)
                else:
                    print(f"[tick {iteration}] handled {handled}, retick immediately")
                    if max_iters and iteration >= max_iters:
                        break
        except KeyboardInterrupt:
            print("\nShutting down.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Multi-agent team orchestrator for m3-memory")
    parser.add_argument("team", nargs="?", default="team.yaml", help="path to team.yaml")
    args = parser.parse_args()
    asyncio.run(main(Path(args.team)))
