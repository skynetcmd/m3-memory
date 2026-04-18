#!/usr/bin/env python3
"""
MCP Tool Execution Proxy  v2.0
==============================
OpenAI-compatible server on localhost:9000.

Purpose
-------
Aider and OpenClaw have no native MCP support. This proxy sits between them
and the actual model, injecting MCP tools into every request and executing
tool_calls by calling bridge functions directly — no MCP transport overhead.

Tool sources (v2)
-----------------
1. Operational Protocol tools from custom_tool_bridge (5):
   log_activity, query_decisions, update_focus, retire_focus, check_thermal_load
2. Debug Agent tools from debug_agent_bridge (6):
   debug_analyze, debug_bisect, debug_trace, debug_correlate, debug_history, debug_report
3. m3-memory catalog tools from mcp_tool_catalog.TOOLS (44):
   memory_*, agent_*, task_*, conversation_*, notifications_*, etc.

Default allowlist excludes destructive catalog tools (memory_delete, chroma_sync,
gdpr_*, *_export, *_import, memory_maintenance, memory_set_retention, agent_offline).
Set MCP_PROXY_ALLOW_DESTRUCTIVE=1 to expose them.

Agent identity is taken from the X-Agent-Id request header (default
"mcp-proxy-client"). Catalog tools marked inject_agent_id receive this value
non-bypassably; the LLM cannot spoof another agent.

Routing
-------
  model contains "claude"   →  Anthropic Messages API  api.anthropic.com
  model contains "gemini"   →  Google AI (OAI-compat)  generativelanguage.googleapis.com
  model contains "grok"     →  xAI (OAI-compat)        api.x.ai
  model contains "sonar"    →  Perplexity (OAI-compat) api.perplexity.ai
  everything else           →  LM Studio (OAI-compat)  localhost:1234

Tool loop
---------
  Requests are forwarded with MCP tools injected.
  If the model returns tool_calls, they are executed in parallel, results
  fed back, and the loop repeats — up to MAX_TOOL_ROUNDS.
  The final tool-call-free response is returned to the client.

Usage
-----
  python3 ./bin/mcp_proxy.py
  # or
  bash ./bin/start_mcp_proxy.sh

Configure clients:
  Aider aider-local:  --openai-api-base http://localhost:9000/v1
  Aider + Claude:     --model openai/claude-sonnet-4-6 --openai-api-base http://localhost:9000/v1
  OpenClaw:           OPENAI_BASE_URL=http://localhost:9000/v1 (see claw-proxy in .zshrc)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from typing import Any, AsyncIterator, List, Optional, Union

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [mcp-proxy] %(levelname)s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mcp_proxy")

from m3_sdk import M3Context

ctx = M3Context()

# ── Constants ─────────────────────────────────────────────────────────────────
PROXY_HOST = os.environ.get("MCP_PROXY_HOST", "127.0.0.1")
PROXY_PORT = 9000
ANTHROPIC_BASE = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
GOOGLE_AI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
GROK_BASE = "https://api.x.ai/v1"
PERPLEXITY_BASE = "https://api.perplexity.ai"
LMSTUDIO_BASE = os.environ.get("LM_STUDIO_BASE", "http://localhost:1234/v1")
MAX_TOOL_ROUNDS = 10
CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = float(os.environ.get("LM_READ_TIMEOUT", "300"))
ALLOW_DESTRUCTIVE = os.environ.get("MCP_PROXY_ALLOW_DESTRUCTIVE", "").lower() in ("1", "true", "yes")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKSPACE = BASE_DIR
sys.path.insert(0, os.path.join(WORKSPACE, "bin"))

# ── Auth ──────────────────────────────────────────────────────────────────────

def _get_proxy_key() -> str:
    """Resolve the master proxy key. Required for all inbound requests."""
    return ctx.get_secret("MCP_PROXY_KEY") or ""


def _check_auth(request: Request):
    """Simple Bearer token check. Returns True if authorized."""
    master = _get_proxy_key()
    if not master:
        # If no key is set, allow (for local development/first-time setup)
        # However, a warning should be logged.
        log.warning("MCP_PROXY_KEY is NOT set. Proxy is running without authentication!")
        return True

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False

    token = auth_header.replace("Bearer ", "").strip()
    return token == master


def _get_key(service: str) -> str:
    """Resolve secret cross-platform: env var → keyring → encrypted vault."""
    return ctx.get_secret(service) or ""


def _anthropic_key() -> str:
    key = _get_key("ANTHROPIC_API_KEY")
    if not key:
        key = _get_key("sk-ant")
    return key


def _lmstudio_key() -> str:
    key = _get_key("LM_STUDIO_API_KEY")
    if not key:
        key = _get_key("LM_API_TOKEN")
    return key or "lm-studio"   # LM Studio accepts any non-empty key


def _google_key() -> str:
    return _get_key("GEMINI_API_KEY")


def _grok_key() -> str:
    return _get_key("XAI_API_KEY")


def _perplexity_key() -> str:
    return _get_key("PERPLEXITY_API_KEY")

# ── Bridge imports (lazy — avoids side effects at module load) ─────────────────

_custom_mod = None
_memory_mod = None
_debug_mod = None


def _custom():
    global _custom_mod
    if _custom_mod is None:
        try:
            import custom_tool_bridge as m
            _custom_mod = m
            log.info("custom_tool_bridge imported OK")
        except Exception as exc:
            log.error(f"Failed to import custom_tool_bridge: {type(exc).__name__}: {exc}")
            raise
    return _custom_mod


def _memory():
    global _memory_mod
    if _memory_mod is None:
        try:
            import memory_bridge as m
            _memory_mod = m
            log.info("memory_bridge imported OK")
        except Exception as exc:
            log.error(f"Failed to import memory_bridge: {type(exc).__name__}: {exc}")
            raise
    return _memory_mod


def _debug():
    global _debug_mod
    if _debug_mod is None:
        try:
            import debug_agent_bridge as m
            _debug_mod = m
            log.info("debug_agent_bridge imported OK")
        except Exception as exc:
            log.error(f"Failed to import debug_agent_bridge: {type(exc).__name__}: {exc}")
            raise
    return _debug_mod

# ── MCP tool definitions ──────────────────────────────────────────────────────
#
# Tools come from three sources:
#   1. PROTOCOL_TOOLS — 5 Operational Protocol tools from custom_tool_bridge
#   2. DEBUG_TOOLS — 6 debug agent tools from debug_agent_bridge
#   3. mcp_tool_catalog.TOOLS — 44 m3-memory tools (the v2 single source of truth)
#
# Sources 1 and 2 are kept inline because their bridges aren't catalog-registered.
# Source 3 is built dynamically each request from mcp_tool_catalog and respects
# default_allowed (destructive tools opt-in via MCP_PROXY_ALLOW_DESTRUCTIVE) and
# inject_agent_id (the orchestrator stamps agent_id; the LLM cannot spoof).

PROTOCOL_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "log_activity",
            "description": (
                "Archive activity to the agent log (Protocols #1-#3). "
                "category=thought for complex reasoning, hardware after thermal check, "
                "decision when user agrees to any code change, file move, or direction."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {"type": "string", "enum": ["thought", "hardware", "decision"]},
                    "detail_a": {"type": "string", "description": "Primary detail (<=500 chars)"},
                    "detail_b": {"type": "string", "description": "Secondary detail (<=2000 chars)"},
                    "detail_c": {"type": "string", "description": "Tertiary detail / root cause"},
                },
                "required": ["category", "detail_a"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_decisions",
            "description": (
                "Protocol #4 - MUST call before starting any new task. "
                "Full-text search across project_decisions table for prior decisions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "Topic keywords for the task"},
                    "limit": {"type": "integer", "default": 10, "description": "Max results"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_focus",
            "description": "Protocol #5 - Call every 3 turns with a <=10-word trajectory summary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "<=10-word current trajectory"},
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "retire_focus",
            "description": "Protocol #5 - Clear dashboard focus when a task completes.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_thermal_load",
            "description": "Protocol #2 - Check M3 Max thermal/RAM pressure. Returns Nominal|Fair|Serious|Critical.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

DEBUG_TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "debug_analyze",
            "description": "Root cause analysis with memory-augmented reasoning. Searches past issues, reads source, uses local LLM to diagnose.",
            "parameters": {
                "type": "object",
                "properties": {
                    "error_message": {"type": "string", "description": "The error message or symptom to analyze"},
                    "context": {"type": "string", "description": "Additional context (stack trace, repro steps)"},
                    "file_path": {"type": "string", "description": "Source file path for context"},
                },
                "required": ["error_message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "debug_bisect",
            "description": "Automated git bisect with LLM analysis of the offending commit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "test_command": {"type": "string", "description": "Shell command that exits 0 on success"},
                    "good_commit": {"type": "string", "description": "Known-good commit hash or ref"},
                    "bad_commit": {"type": "string", "default": "HEAD", "description": "Known-bad commit"},
                },
                "required": ["test_command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "debug_trace",
            "description": "Execution flow analysis - reads source, finds callers, identifies failure points.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to the source file"},
                    "function_name": {"type": "string", "description": "Function to focus on"},
                    "error_type": {"type": "string", "description": "Error type to look for"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "debug_correlate",
            "description": "Cross-reference logs, git commits, and decisions to build a causal timeline.",
            "parameters": {
                "type": "object",
                "properties": {
                    "log_file": {"type": "string", "description": "Log file path to parse"},
                    "time_range": {"type": "string", "default": "24h", "description": "Time window (e.g. 1h, 24h, 7d)"},
                    "pattern": {"type": "string", "description": "Regex pattern to filter log entries"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "debug_history",
            "description": "Search past debugging sessions and patterns. No LLM required.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "Search term"},
                    "limit": {"type": "integer", "default": 10, "description": "Max results"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "debug_report",
            "description": "Generate and persist a structured debugging report to memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue/ticket ID"},
                    "title": {"type": "string", "description": "Report title (required)"},
                    "findings": {"type": "string", "description": "Debugging findings and resolution"},
                },
                "required": ["title"],
            },
        },
    },
]

# Catalog import — done lazily to avoid pulling memory_core at module load
_catalog_mod = None
_catalog_specs_by_name: dict = {}
_catalog_tool_schemas: list[dict] = []
_catalog_built = False


def _build_catalog_tools() -> tuple[list[dict], dict]:
    """Lazy-load mcp_tool_catalog and build (openai_schemas, name->spec)."""
    global _catalog_mod, _catalog_specs_by_name, _catalog_tool_schemas, _catalog_built
    if _catalog_built:
        return _catalog_tool_schemas, _catalog_specs_by_name
    try:
        import mcp_tool_catalog as cat
        _catalog_mod = cat
        for spec in cat.TOOLS:
            if not ALLOW_DESTRUCTIVE and not spec.default_allowed:
                continue
            _catalog_specs_by_name[spec.name] = spec
            _catalog_tool_schemas.append({
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            })
        log.info(
            f"Catalog loaded: {len(_catalog_tool_schemas)} tools "
            f"(ALLOW_DESTRUCTIVE={ALLOW_DESTRUCTIVE})"
        )
    except Exception as exc:
        log.error(f"Failed to load mcp_tool_catalog: {type(exc).__name__}: {exc}")
    _catalog_built = True
    return _catalog_tool_schemas, _catalog_specs_by_name


def get_mcp_tools() -> list[dict]:
    """Return the merged tool list: protocol + debug + catalog (filtered)."""
    catalog_schemas, _ = _build_catalog_tools()
    return PROTOCOL_TOOLS + DEBUG_TOOLS + catalog_schemas


# Backwards compat — some external callers (start scripts, health checks)
# may still reference MCP_TOOLS as a module attribute. Keep it as a property-
# like cached list. Note: this is built once at first access.
class _LazyToolList(list):
    def __init__(self):
        super().__init__()
        self._loaded = False

    def _load(self):
        if not self._loaded:
            self.extend(get_mcp_tools())
            self._loaded = True

    def __len__(self):
        self._load()
        return super().__len__()

    def __iter__(self):
        self._load()
        return super().__iter__()

    def __getitem__(self, idx):
        self._load()
        return super().__getitem__(idx)


MCP_TOOLS = _LazyToolList()


# ── Tool executor ─────────────────────────────────────────────────────────────

# Map of protocol/debug tool name -> (bridge_loader, attr_name, is_async)
_LEGACY_DISPATCH = {
    "log_activity":       (lambda: _custom(), "log_activity",       True),
    "query_decisions":    (lambda: _custom(), "query_decisions",    True),
    "update_focus":       (lambda: _custom(), "update_focus",       True),
    "retire_focus":       (lambda: _custom(), "retire_focus",       True),
    "check_thermal_load": (lambda: _custom(), "check_thermal_load", True),
    "debug_analyze":      (lambda: _debug(),  "debug_analyze",      True),
    "debug_bisect":       (lambda: _debug(),  "debug_bisect",       True),
    "debug_trace":        (lambda: _debug(),  "debug_trace",        True),
    "debug_correlate":    (lambda: _debug(),  "debug_correlate",    True),
    "debug_history":      (lambda: _debug(),  "debug_history",      False),
    "debug_report":       (lambda: _debug(),  "debug_report",       False),
}


async def _execute_tool(name: str, args: dict, agent_id: str = "mcp-proxy-client") -> str:
    """Dispatch a tool_call to the right bridge.

    Order:
      1. Legacy protocol/debug tools -> direct bridge call
      2. Catalog tools -> mcp_tool_catalog.execute_tool (handles validation,
         agent_id injection, async/sync, error capture)
    """
    try:
        if name in _LEGACY_DISPATCH:
            loader, attr, is_async = _LEGACY_DISPATCH[name]
            bridge = loader()
            fn = getattr(bridge, attr)
            result = await fn(**args) if is_async else fn(**args)
            return result if isinstance(result, str) else str(result)

        _, specs_by_name = _build_catalog_tools()
        spec = specs_by_name.get(name)
        if spec is None:
            if _catalog_mod is not None:
                full = _catalog_mod.get_tool(name)
                if full is not None and not ALLOW_DESTRUCTIVE and not full.default_allowed:
                    return (
                        f"Tool '{name}' is destructive and disabled by default. "
                        f"Set MCP_PROXY_ALLOW_DESTRUCTIVE=1 to enable."
                    )
            return f"Unknown MCP tool: {name}"

        return await _catalog_mod.execute_tool(spec, args, agent_id)
    except Exception as exc:
        log.warning(f"Tool {name} raised {type(exc).__name__}: {exc}")
        return f"Tool error: {type(exc).__name__}: {exc}"

# ── Routing ───────────────────────────────────────────────────────────────────

def _bare_model(model: str) -> str:
    """Strip provider prefix (openai/, anthropic/, google/, etc.)."""
    return model.split("/")[-1] if "/" in model else model


def _route(model: str) -> tuple[str, str, str]:
    """
    Return (backend_type, base_url, api_key) for a given model name.
    backend_type is "anthropic" or "openai_compat".
    """
    base = _bare_model(model).lower()
    if "claude" in base:
        return "anthropic", ANTHROPIC_BASE, _anthropic_key()
    if "gemini" in base:
        return "openai_compat", GOOGLE_AI_BASE, _google_key()
    if "grok" in base:
        return "openai_compat", GROK_BASE, _grok_key()
    if "sonar" in base or "perplexity" in base:
        return "openai_compat", PERPLEXITY_BASE, _perplexity_key()
    return "openai_compat", LMSTUDIO_BASE, _lmstudio_key()

# ── OpenAI ↔ Anthropic format adapters ───────────────────────────────────────

def _tools_oai_to_anthropic(tools: list) -> list:
    out = []
    for t in tools:
        f = t.get("function", {})
        out.append({
            "name": f["name"],
            "description": f.get("description", ""),
            "input_schema": f.get("parameters", {"type": "object", "properties": {}}),
        })
    return out


def _messages_oai_to_anthropic(messages: list) -> tuple[str, list]:
    """
    Convert OpenAI messages list to (system_prompt, anthropic_messages).
    Handles: system, user, assistant (with tool_calls), tool results.
    """
    system_parts: list[str] = []
    converted: list[dict] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content")

        if role == "system":
            if content:
                system_parts.append(str(content))
            continue

        if role == "tool":
            # OpenAI tool result → Anthropic tool_result block inside user message
            converted.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": str(content or ""),
                }],
            })
            continue

        if role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                blocks: list[dict] = []
                if content:
                    blocks.append({"type": "text", "text": str(content)})
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    raw_args = fn.get("arguments", "{}")
                    try:
                        inp = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        inp = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", f"tu_{uuid.uuid4().hex[:8]}"),
                        "name": fn.get("name", ""),
                        "input": inp,
                    })
                converted.append({"role": "assistant", "content": blocks})
                continue

        # Standard message (user or assistant without tool_calls)
        if isinstance(content, list):
            converted.append({"role": role, "content": content})
        else:
            converted.append({"role": role, "content": str(content or "")})

    return "\n".join(system_parts).strip(), converted


def _anthropic_resp_to_oai(resp: dict, model: str) -> dict:
    """Convert Anthropic Messages response to OpenAI chat.completion format."""
    content_blocks = resp.get("content", [])
    text_parts: list[str] = []
    tool_calls: list[dict] = []

    for block in content_blocks:
        btype = block.get("type")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {})),
                },
            })

    text = "".join(text_parts) or None
    stop = resp.get("stop_reason", "end_turn")
    finish = "tool_calls" if stop == "tool_use" else ("length" if stop == "max_tokens" else "stop")

    message: dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls

    usage = resp.get("usage", {})
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }

# ── Backend callers ───────────────────────────────────────────────────────────

_http_timeout = httpx.Timeout(
    connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=10.0, pool=5.0,
)


# ── Shared HTTP Client ────────────────────────────────────────────────────────

_shared_client: httpx.AsyncClient | None = None

def _get_client() -> httpx.AsyncClient:
    """Return a shared httpx.AsyncClient with connection pooling."""
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=10.0, pool=5.0),
            http2=True,
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100)
        )
    return _shared_client


async def _call_anthropic(
    messages: list, model: str, tools: list, max_tokens: int
) -> dict:
    api_key = _anthropic_key()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not found in env or Keychain")

    system, converted = _messages_oai_to_anthropic(messages)
    anthropic_tools = _tools_oai_to_anthropic(tools) if tools else []

    payload: dict[str, Any] = {
        "model": _bare_model(model),
        "messages": converted,
        "max_tokens": max_tokens,
    }
    if system:
        payload["system"] = system
    if anthropic_tools:
        payload["tools"] = anthropic_tools
        payload["tool_choice"] = {"type": "auto"}

    headers = {
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    client = _get_client()
    resp = await client.post(f"{ANTHROPIC_BASE}/messages", json=payload, headers=headers)
    resp.raise_for_status()
    return _anthropic_resp_to_oai(resp.json(), model)


async def _call_openai_compat(
    base_url: str, api_key: str, messages: list, model: str, tools: list, max_tokens: int
) -> dict:
    """Generic caller for OpenAI-compatible backends: Google AI, Grok, Perplexity, LM Studio."""
    payload: dict[str, Any] = {
        "model": _bare_model(model),
        "messages": messages,
        "stream": False,
        "max_tokens": max_tokens,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    headers = {
        "Authorization": f"Bearer {api_key}",
        "content-type": "application/json",
    }

    client = _get_client()
    resp = await client.post(
        f"{base_url}/chat/completions", json=payload, headers=headers
    )
    resp.raise_for_status()
    return resp.json()

# ── Core tool execution loop ──────────────────────────────────────────────────

async def _run_with_tools(
    messages: list,
    model: str,
    client_tools: list,
    max_tokens: int,
    agent_id: str = "mcp-proxy-client",
) -> dict:
    """
    Main loop:
      1. Inject MCP tools alongside any tools the client already sent.
      2. Call the appropriate backend (no streaming — we need to inspect for tool_calls).
      3. Execute tool_calls in parallel, append results, repeat.
      4. Return the first response with finish_reason != tool_calls.
    """
    all_tools = get_mcp_tools() + client_tools
    working = list(messages)
    btype, burl, bkey = _route(model)

    for round_num in range(MAX_TOOL_ROUNDS):
        if btype == "anthropic":
            response = await _call_anthropic(working, model, all_tools, max_tokens)
        else:
            response = await _call_openai_compat(burl, bkey, working, model, all_tools, max_tokens)

        choice = response.get("choices", [{}])[0]
        message = choice.get("message", {})
        finish = choice.get("finish_reason", "stop")
        tool_calls: list = message.get("tool_calls") or []

        if not tool_calls or finish != "tool_calls":
            log.info(f"Done in {round_num + 1} round(s) - finish_reason={finish}")
            return response

        log.info(f"Round {round_num + 1}: executing {len(tool_calls)} tool call(s) for agent={agent_id}")

        # Add assistant message (with tool_calls) to history
        working.append(message)

        # Execute all tools concurrently (bounded)
        _tool_sem = asyncio.Semaphore(5)

        async def _exec_one(tc: dict) -> dict:
            async with _tool_sem:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                raw = fn.get("arguments", "{}")
                try:
                    args = json.loads(raw) if isinstance(raw, str) else raw
                except json.JSONDecodeError:
                    args = {}
                log.info(f"  -> {name}({list(args.keys())})")
                try:
                    result = await asyncio.wait_for(
                        _execute_tool(name, args, agent_id), timeout=300.0
                    )
                except asyncio.TimeoutError:
                    log.warning(f"Tool {name} timed out after 300s")
                    result = f"Tool error: {name} timed out after 300s"
                return {
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": str(result),
                }

        tool_results = await asyncio.gather(*[_exec_one(tc) for tc in tool_calls])
        working.extend(tool_results)

    log.warning(f"Reached MAX_TOOL_ROUNDS ({MAX_TOOL_ROUNDS}) - returning last response")
    return response  # type: ignore[return-value]

# ── SSE streaming adapter ─────────────────────────────────────────────────────

async def _sse_from_completion(completion: dict) -> AsyncIterator[str]:
    """
    Convert a complete chat.completion dict into SSE chunks.
    Splits content into ~32-char chunks so the client sees incremental output.
    Tool calls are emitted as a single chunk (they aren't streamed).
    Ends with a [DONE] sentinel.
    """
    cid = completion.get("id", f"chatcmpl-{uuid.uuid4().hex[:8]}")
    model = completion.get("model", "")
    created = completion.get("created") or int(time.time())
    choice = completion.get("choices", [{}])[0]
    message = choice.get("message", {})
    finish = choice.get("finish_reason", "stop")
    content: str = message.get("content") or ""
    tool_calls: list = message.get("tool_calls") or []

    def _chunk(delta: dict, finish_reason=None) -> str:
        c = {"index": 0, "delta": delta, "finish_reason": finish_reason}
        payload = {"id": cid, "object": "chat.completion.chunk",
                   "created": created, "model": model, "choices": [c]}
        return f"data: {json.dumps(payload)}\n\n"

    # Role chunk
    yield _chunk({"role": "assistant"})

    if tool_calls:
        # Emit tool calls in a single chunk (not split)
        yield _chunk({"tool_calls": tool_calls})
    elif content:
        # Stream content in ~32-char pieces
        chunk_size = 32
        for i in range(0, len(content), chunk_size):
            yield _chunk({"content": content[i:i + chunk_size]})

    # Finish chunk
    yield _chunk({}, finish_reason=finish)
    yield "data: [DONE]\n\n"


# ── FastAPI application ───────────────────────────────────────────────────────

app = FastAPI(
    title="MCP Tool Execution Proxy",
    description="Injects m3-memory v2 catalog + protocol/debug tools into Aider/OpenClaw requests.",
    version="2.0.0",
)


@app.on_event("shutdown")
async def _on_shutdown():
    global _shared_client
    if _shared_client and not _shared_client.is_closed:
        await _shared_client.aclose()
        log.info("Shared HTTP client closed.")


@app.get("/health")
async def health() -> dict:
    catalog_schemas, _ = _build_catalog_tools()
    return {
        "status": "ok",
        "proxy": "mcp-tool-proxy",
        "version": "2.0.0",
        "tool_counts": {
            "protocol": len(PROTOCOL_TOOLS),
            "debug": len(DEBUG_TOOLS),
            "catalog": len(catalog_schemas),
            "total": len(PROTOCOL_TOOLS) + len(DEBUG_TOOLS) + len(catalog_schemas),
        },
        "allow_destructive": ALLOW_DESTRUCTIVE,
        # Backwards-compat field — old health checks looked for "mcp_tools"
        "mcp_tools": len(PROTOCOL_TOOLS) + len(DEBUG_TOOLS) + len(catalog_schemas),
        "backends": {
            "anthropic": ANTHROPIC_BASE,
            "google": GOOGLE_AI_BASE,
            "grok": GROK_BASE,
            "perplexity": PERPLEXITY_BASE,
            "lmstudio": LMSTUDIO_BASE,
        },
    }


@app.get("/v1/models")
async def list_models(request: Request) -> dict:
    """Return known Claude models + whatever LM Studio currently has loaded."""
    if not _check_auth(request):
        raise HTTPException(status_code=401, detail="Unauthorized: MCP_PROXY_KEY mismatch")

    models: list[dict] = [
        {"id": "claude-sonnet-4-6", "object": "model", "owned_by": "anthropic"},
        {"id": "claude-opus-4-6", "object": "model", "owned_by": "anthropic"},
        {"id": "claude-haiku-4-5-20251001", "object": "model", "owned_by": "anthropic"},
        {"id": "gemini-2.0-flash", "object": "model", "owned_by": "google"},
        {"id": "gemini-2.0-flash-exp", "object": "model", "owned_by": "google"},
        {"id": "grok-3-latest", "object": "model", "owned_by": "xai"},
        {"id": "grok-3", "object": "model", "owned_by": "xai"},
        {"id": "sonar-pro", "object": "model", "owned_by": "perplexity"},
    ]
    try:
        client = ctx.get_async_client()
        r = await client.get(f"{LMSTUDIO_BASE}/models", timeout=2.0)
        if r.status_code == 200:
            models.extend(r.json().get("data", []))
    except Exception:
        pass
    return {"object": "list", "data": models}


# ── Pydantic Models ───────────────────────────────────────────────────────────

class ChatMessage(BaseModel):
    role: str
    content: Optional[Union[str, list]] = None
    tool_calls: Optional[list] = None
    tool_call_id: Optional[str] = None
    name: Optional[str] = None

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    tools: Optional[List[dict]] = None
    stream: Optional[bool] = False
    max_tokens: Optional[int] = Field(default=None, ge=0)
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 1.0
    n: Optional[int] = 1

@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> JSONResponse:
    if not _check_auth(request):
        return JSONResponse(
            status_code=401,
            content={"error": {"message": "Unauthorized: MCP_PROXY_KEY mismatch", "type": "auth_error"}},
        )
    try:
        raw_body = await request.json()
        body = ChatCompletionRequest(**raw_body)
    except Exception as exc:
        log.error(f"Invalid request body: {exc}")
        return JSONResponse(
            status_code=400,
            content={"error": {"message": f"Invalid request: {exc}", "type": "invalid_request"}},
        )

    model = body.model
    messages = [m.model_dump(exclude_none=True) for m in body.messages]
    client_tools = body.tools or []
    wants_stream = body.stream
    agent_id = request.headers.get("X-Agent-Id", "mcp-proxy-client")

    # Fix M8: Correctly handle max_tokens=0 or missing
    if body.max_tokens is not None and body.max_tokens > 0:
        max_tokens = body.max_tokens
    else:
        max_tokens = (8096 if "claude" in model else 32768)

    btype, burl, _ = _route(model)
    log.info(
        f"Request: model={model} backend={btype} url={burl.split('/')[2]} "
        f"agent={agent_id} messages={len(messages)} client_tools={len(client_tools)} stream={wants_stream}"
    )

    try:
        result = await _run_with_tools(messages, model, client_tools, max_tokens, agent_id)

        if wants_stream:
            return StreamingResponse(
                _sse_from_completion(result),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        return JSONResponse(content=result)

    except httpx.ConnectError:
        log.error("Backend unreachable")
        return JSONResponse(
            status_code=503,
            content={"error": {"message": "Backend unreachable", "type": "connection_error"}},
        )
    except httpx.ReadTimeout:
        log.error("Backend read timeout")
        return JSONResponse(
            status_code=504,
            content={"error": {"message": "Backend read timeout", "type": "timeout_error"}},
        )
    except httpx.HTTPStatusError as exc:
        log.error(f"Backend HTTP {exc.response.status_code}")
        return JSONResponse(
            status_code=exc.response.status_code,
            content={"error": {
                "message": exc.response.text[:300],
                "type": "backend_error",
            }},
        )
    except ValueError as exc:
        log.error(f"Config error: {exc}")
        return JSONResponse(
            status_code=500,
            content={"error": {"message": "Configuration error. Check server logs.", "type": "config_error"}},
        )
    except Exception as exc:
        log.error(f"Unexpected: {type(exc).__name__}")
        return JSONResponse(
            status_code=500,
            content={"error": {"message": type(exc).__name__, "type": "proxy_error"}},
        )


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    catalog_schemas, _ = _build_catalog_tools()
    total_tools = len(PROTOCOL_TOOLS) + len(DEBUG_TOOLS) + len(catalog_schemas)
    log.info("=" * 60)
    log.info("  MCP Tool Execution Proxy  v2.0")
    log.info(f"  Listening:          http://{PROXY_HOST}:{PROXY_PORT}")
    log.info(f"  LM Studio:          {LMSTUDIO_BASE}")
    log.info(f"  Anthropic:          {ANTHROPIC_BASE}")
    log.info(f"  Protocol tools:     {len(PROTOCOL_TOOLS)}")
    log.info(f"  Debug tools:        {len(DEBUG_TOOLS)}")
    log.info(f"  Catalog tools:      {len(catalog_schemas)} (ALLOW_DESTRUCTIVE={ALLOW_DESTRUCTIVE})")
    log.info(f"  Total injected:     {total_tools}")
    log.info("=" * 60)
    uvicorn.run(app, host=PROXY_HOST, port=PROXY_PORT, log_level="warning")

