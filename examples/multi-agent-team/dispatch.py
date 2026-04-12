"""
dispatch.py — multi-turn MCP dispatch loop for the multi-agent team orchestrator.

Called once per notification by orchestrator.py. Turns an LLM into a real MCP
client: builds an OpenAI-shape tools list from the allowlist, calls the provider
with tool calling enabled, executes any tool calls through mcp_tool_catalog,
appends results to the message history, and loops until the model emits no more
tool calls (or a bound is hit).

What dispatch OWNS: the per-turn provider call, the tool call fan-out, allowlist
enforcement, loop/bound detection, retry policy, and task_update_impl on
bounded failures.

What dispatch does NOT own: notification polling, concurrency across
dispatches, ack on terminal, run-log writing, and console output. Those all
belong to the orchestrator, which sees only the returned DispatchResult.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# Resolve repo root so we can import bin/ modules without installing the package.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "bin"))

import memory_core              # noqa: E402
import mcp_tool_catalog         # noqa: E402
from agent_protocol import AgentProtocol  # noqa: E402


@dataclass
class DispatchLimits:
    max_turns: int = 8
    max_tool_calls: int = 24
    max_seconds: float = 120.0
    max_tokens_per_call: int = 4096
    provider_retries: int = 3


@dataclass
class DispatchResult:
    terminal: str
    final_text: str = ""
    turns: int = 0
    total_tool_calls: int = 0
    elapsed_seconds: float = 0.0
    error: str | None = None
    messages: list = field(default_factory=list)


class ProviderError4xx(Exception):
    """Non-retryable provider error (config, auth, bad request)."""


class ProviderError5xxExhausted(Exception):
    """Retryable provider error that exhausted the retry budget."""


def build_openai_tools(allowlist: set[str]) -> list[dict]:
    """Build OpenAI-shape tools list from catalog specs matching the allowlist.

    The `parameters` dict is shared with the catalog — do not mutate. The
    Gemini stripper in AgentProtocol already makes a copy at translate time.
    """
    out: list[dict] = []
    for spec in mcp_tool_catalog.TOOLS:
        if spec.name not in allowlist:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        })
    return out


def resolve_allowlist(agent: dict) -> set[str]:
    """Resolve which tools the agent may call.

    team.yaml shape:
        tools:
          allow: [tool1, tool2, ...]   # explicit allowlist (replaces default)
          deny:  [tool3]                # subtractive (applied after allow)
    Either field is optional. Missing `tools` key entirely = catalog default.
    """
    tools_cfg = agent.get("tools") or {}
    if tools_cfg.get("allow"):
        allowed = set(tools_cfg["allow"])
    else:
        allowed = set(mcp_tool_catalog.default_allowlist())
    deny = set(tools_cfg.get("deny") or [])
    return allowed - deny


def _build_initial_messages(agent: dict, notification: dict,
                            task_record: str | None, task_id: str | None) -> list[dict]:
    """System + user message for turn 1. Embeds task_id so the model can pass
    it straight to task_update without digging through the payload."""
    payload = json.loads(notification.get("payload_json") or "{}")
    kind = notification["kind"]

    user_lines = [
        f"You have a new {kind} notification.",
        f"Notification payload: {json.dumps(payload, indent=2)}",
    ]
    if task_id:
        user_lines.append(f"Task id: {task_id}")
    if task_record:
        user_lines += ["", "Task details from m3-memory:", task_record]
    user_lines += [
        "",
        "You have access to m3-memory tools. Use them to:",
        "- Read any context memories the planner attached (memory_search, memory_get).",
        "- Do the work the task describes.",
        "- Write your result to memory (memory_write) so the team can build on it.",
        "- When the work is complete, update the task state with "
        "task_update(task_id, state=\"completed\").",
        "- When you have nothing more to do this turn, return a final text response "
        "(no more tool calls).",
        "",
        "Stay focused. Don't call tools that aren't necessary for this task.",
    ]

    return [
        {"role": "system", "content": agent["system_prompt"]},
        {"role": "user", "content": "\n".join(user_lines)},
    ]


async def _call_provider_with_retry(http: httpx.AsyncClient, provider_cfg: dict,
                                    model: str, messages: list[dict],
                                    tools: list[dict], max_tokens: int,
                                    retries: int) -> dict:
    """Send one provider request; retry on 429/5xx; return the raw response dict.

    Contract differs from v1's call_provider:
      - takes a tools list and max_tokens
      - returns the RAW response dict (so caller can parse tool_calls)
      - raises ProviderError4xx immediately on non-retryable 4xx
      - raises ProviderError5xxExhausted after exhausting retries on 429/5xx
    """
    fmt = provider_cfg["format"]
    api_key = os.getenv(provider_cfg["api_key_env"], "")

    attempt = 0
    last_err: Exception | None = None
    while attempt < max(1, retries):
        try:
            if fmt == "openai_compat":
                headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
                payload: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "max_tokens": max_tokens,
                }
                if tools:
                    payload["tools"] = tools
                resp = await http.post(provider_cfg["base_url"], json=payload, headers=headers)
            elif fmt == "anthropic":
                headers = {
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "Content-Type": "application/json",
                }
                payload = AgentProtocol.openai_to_anthropic_with_tools(
                    messages, model, tools=tools, max_tokens=max_tokens
                )
                resp = await http.post(provider_cfg["base_url"], json=payload, headers=headers)
            elif fmt == "gemini":
                url = f"{provider_cfg['base_url']}/{model}:generateContent?key={api_key}"
                payload = AgentProtocol.openai_to_gemini_with_tools(messages, model, tools=tools)
                resp = await http.post(url, json=payload)
            else:
                raise ValueError(f"Unknown provider format: {fmt!r}")

            status = resp.status_code
            if 200 <= status < 300:
                return resp.json()

            body_text = resp.text or ""
            retryable = (
                status == 429
                or 500 <= status < 600
                or (fmt == "anthropic" and status == 529)
                or (fmt == "gemini" and "RESOURCE_EXHAUSTED" in body_text)
            )
            err_msg = f"{fmt} {status}: {body_text[:400]}"
            if not retryable:
                raise ProviderError4xx(err_msg)
            last_err = Exception(err_msg)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            last_err = e

        attempt += 1
        if attempt >= retries:
            break
        backoff = min(0.5 * (2 ** (attempt - 1)), 4.0)
        await asyncio.sleep(backoff)

    raise ProviderError5xxExhausted(
        f"exhausted {retries} attempts; last error: {last_err}"
    )


async def _terminate(terminal: str, task_id: str | None, agent_name: str,
                     reason: str, messages: list, turns: int,
                     total_calls: int, started: float,
                     error: str | None = None, final_text: str = "",
                     mark_failed: bool = True) -> DispatchResult:
    """Build a DispatchResult; mark the task failed on bounded-failure terminals.

    Success and 5xx-exhausted skip task_update:
      - success: the agent was supposed to call task_update itself
      - 5xx-exhausted: transient — orchestrator will retry next tick
    """
    if mark_failed and task_id and terminal not in ("success", "provider_error_5xx_exhausted"):
        try:
            memory_core.task_update_impl(
                task_id=task_id,
                state="failed",
                metadata={
                    "dispatch_terminal": terminal,
                    "reason": reason,
                    "actor": agent_name,
                },
                actor=agent_name,
            )
        except Exception:
            pass
    return DispatchResult(
        terminal=terminal,
        final_text=final_text,
        turns=turns,
        total_tool_calls=total_calls,
        elapsed_seconds=time.monotonic() - started,
        error=error,
        messages=messages,
    )


async def dispatch(agent: dict, provider_cfg: dict, notification: dict,
                   task_record: str | None, http: httpx.AsyncClient,
                   limits: DispatchLimits) -> DispatchResult:
    """Run one notification through its agent's LLM as a tool-calling MCP client."""
    payload = json.loads(notification.get("payload_json") or "{}")
    task_id = payload.get("task_id")
    agent_name = agent["name"]

    allowlist = resolve_allowlist(agent)
    tools_for_model = build_openai_tools(allowlist)

    messages = _build_initial_messages(agent, notification, task_record, task_id)
    started = time.monotonic()
    total_calls = 0
    turns = 0
    last_call_hashes: list[tuple[str, str]] = []

    for turn in range(1, limits.max_turns + 1):
        turns = turn
        if (time.monotonic() - started) > limits.max_seconds:
            return await _terminate(
                "timeout", task_id, agent_name,
                reason=f"elapsed > {limits.max_seconds}s",
                messages=messages, turns=turns, total_calls=total_calls, started=started,
            )

        try:
            raw = await _call_provider_with_retry(
                http, provider_cfg, agent["model"], messages,
                tools_for_model, limits.max_tokens_per_call, limits.provider_retries,
            )
        except ProviderError4xx as e:
            return await _terminate(
                "provider_error_4xx", task_id, agent_name,
                reason="4xx from provider",
                messages=messages, turns=turns, total_calls=total_calls,
                started=started, error=str(e),
            )
        except ProviderError5xxExhausted as e:
            return await _terminate(
                "provider_error_5xx_exhausted", task_id, agent_name,
                reason="5xx retries exhausted",
                messages=messages, turns=turns, total_calls=total_calls,
                started=started, error=str(e), mark_failed=False,
            )

        text, tool_calls = AgentProtocol.parse_tool_calls(raw, provider_cfg["format"])

        assistant_msg: dict[str, Any] = {"role": "assistant", "content": text or ""}
        if tool_calls:
            assistant_msg["tool_calls"] = tool_calls
        messages.append(assistant_msg)

        if not tool_calls:
            return DispatchResult(
                terminal="success",
                final_text=text or "",
                turns=turns,
                total_tool_calls=total_calls,
                elapsed_seconds=time.monotonic() - started,
                error=None,
                messages=messages,
            )

        for tc in tool_calls:
            total_calls += 1
            if total_calls > limits.max_tool_calls:
                return await _terminate(
                    "tool_call_budget", task_id, agent_name,
                    reason=f"{total_calls} > {limits.max_tool_calls}",
                    messages=messages, turns=turns, total_calls=total_calls, started=started,
                )

            fn = tc.get("function") or {}
            tool_name = fn.get("name", "")
            tool_args = AgentProtocol._safe_json_loads(fn.get("arguments", "{}"))

            call_hash = (tool_name, json.dumps(tool_args, sort_keys=True, default=str))
            last_call_hashes.append(call_hash)
            if len(last_call_hashes) > 4:
                last_call_hashes.pop(0)
            if len(last_call_hashes) == 4 and len(set(last_call_hashes)) == 1:
                return await _terminate(
                    "loop_detected", task_id, agent_name,
                    reason=f"4x same call: {tool_name}",
                    messages=messages, turns=turns, total_calls=total_calls, started=started,
                )

            if tool_name not in allowlist:
                result = (
                    f"Error: tool '{tool_name}' is not in the allowlist for agent "
                    f"'{agent_name}'. Allowed tools: {', '.join(sorted(allowlist))}."
                )
                is_error = True
            else:
                spec = mcp_tool_catalog.get_tool(tool_name)
                if spec is None:
                    result = f"Error: tool '{tool_name}' does not exist."
                    is_error = True
                else:
                    result = await mcp_tool_catalog.execute_tool(spec, tool_args, agent_name)
                    is_error = isinstance(result, str) and result.startswith("Error:")

            tool_msg = AgentProtocol.format_tool_result(
                tc.get("id", ""), tool_name, result, is_error
            )
            messages.append(tool_msg)

    return await _terminate(
        "max_turns", task_id, agent_name,
        reason=f"hit {limits.max_turns} turns",
        messages=messages, turns=turns, total_calls=total_calls, started=started,
    )
