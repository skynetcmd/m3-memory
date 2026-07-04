"""catalog.dispatch — execute_tool / execute_tool_structured, timeout
machinery, and the m3_call / m3_index dispatcher.

Moved (mostly verbatim) from mcp_tool_catalog.py as part of the catalog/
subpackage split. Several functions here need the AGGREGATED `TOOLS` list
and `_BY_NAME` map, which only exist on `mcp_tool_catalog` itself (built
after this module partition is assembled) — those import `mcp_tool_catalog`
LAZILY inside the function body to avoid a circular import at module-load
time (mcp_tool_catalog imports this module at its own module top).
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from m3_sdk import active_database

from .spec import ToolSpec, memory_core


# ── Helpers ──────────────────────────────────────────────────────────────────
def get_tool(name: str) -> ToolSpec | None:
    import mcp_tool_catalog
    return mcp_tool_catalog._BY_NAME.get(name)

def default_allowlist() -> set[str]:
    import mcp_tool_catalog
    return {t.name for t in mcp_tool_catalog.TOOLS if t.default_allowed}

def _pop_database(args: dict) -> str | None:
    """Pop the universal `database` arg so validators/impls never see it.

    The field is injected into every ToolSpec.parameters at module end (see
    ``_inject_database_arg``); MCP clients and direct Python callers can pass
    it to target a non-default SQLite DB. Empty string is treated as absent.
    """
    db = args.pop("database", None)
    if isinstance(db, str):
        db = db.strip() or None
    return db


def validate_args(spec: ToolSpec, args: dict) -> tuple[dict, str | None]:
    for v in spec.validators:
        result = v(args)
        if isinstance(result, str) and result.startswith("Error:"):
            return args, result
        if isinstance(result, dict):
            args = result
    return args, None

# ── Per-call timeout (§6 hardening: strict timeouts everywhere) ───────────────
# Every MCP tool call is bounded so a slow/hung impl can never block the server
# or a headless daemon indefinitely (the FILES-search hang, 2026-07-01).
# Precedence, most-specific first:
#   1. per-call `timeout` arg (seconds) — user-selectable per invocation
#   2. M3_TOOL_TIMEOUT env — global default override
#   3. spec.timeout_s — per-tool default (generous on known-slow GPU/batch tools)
#   4. _DEFAULT_TOOL_TIMEOUT (30s)
# A value <= 0 disables the timeout (the "unless otherwise specified" escape
# hatch for genuinely long-running ops, e.g. bench/enrich). Only async impls are
# bounded — a sync impl runs inline on the event loop and cannot be cancelled by
# wait_for; sync tools are simple/fast by construction.
_DEFAULT_TOOL_TIMEOUT = 30.0


def _resolve_tool_timeout(args: dict, spec: "ToolSpec | None" = None) -> float | None:
    """Return the timeout in seconds for this call, or None to disable it.

    Pops the reserved `timeout` key from `args` (so it never reaches the impl).
    Precedence: per-call arg > M3_TOOL_TIMEOUT env > spec.timeout_s > 30s default.
    <=0 disables. Malformed values fall through to the next source (fail safe,
    §3). The per-tool default (spec.timeout_s) lets known-slow tools raise their
    own ceiling without weakening the fast-fail 30s for everything else."""
    raw = args.pop("timeout", None)
    if raw is None:
        raw = os.environ.get("M3_TOOL_TIMEOUT")
    if raw is None and spec is not None and spec.timeout_s is not None:
        raw = spec.timeout_s
    try:
        val = float(raw) if raw is not None and raw != "" else _DEFAULT_TOOL_TIMEOUT
    except (TypeError, ValueError):
        val = _DEFAULT_TOOL_TIMEOUT
    return None if val <= 0 else val


class ToolTimeout(Exception):
    """Raised when a tool impl exceeds its resolved timeout."""


async def _run_impl_bounded(spec: ToolSpec, args: dict, timeout: float | None) -> Any:
    """Invoke spec.impl, enforcing `timeout` on async impls. Fails loud (§3):
    a timeout raises ToolTimeout with the tool name and budget, not a silent
    hang or a bare None."""
    if spec.is_async:
        if timeout is None:
            return await spec.impl(**args)
        try:
            return await asyncio.wait_for(spec.impl(**args), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError) as e:
            raise ToolTimeout(
                f"{spec.name} exceeded {timeout:g}s timeout "
                f"(raise with a larger `timeout` arg or M3_TOOL_TIMEOUT, "
                f"or set <=0 to disable)"
            ) from e
    return spec.impl(**args)


async def execute_tool(spec: ToolSpec, args: dict, agent_id: str) -> str:
    try:
        args = dict(args or {})
        timeout = _resolve_tool_timeout(args, spec)  # pops reserved `timeout` key
        allowed_keys = set(spec.parameters.get("properties", {}).keys())
        args = {k: v for k, v in args.items() if k in allowed_keys}
        database = _pop_database(args)
        if spec.inject_agent_id and "agent_id" in allowed_keys:
            args["agent_id"] = agent_id
        args, err = validate_args(spec, args)
        if err:
            return err
        with active_database(database):
            result = await _run_impl_bounded(spec, args, timeout)
        return result if isinstance(result, str) else str(result)
    except Exception as e:
        return f"Error: {type(e).__name__}: {e}"


async def execute_tool_structured(
    spec: ToolSpec, args: dict, agent_id: str, *, dry_run: bool = False
) -> Any:
    """Like execute_tool, but returns the impl's NATIVE return value (dict/list/
    etc.) instead of str()-coercing it, and raises on error instead of returning
    an "Error: …" string. This is the path the m3_call dispatcher uses so the LLM
    receives parseable structure (m3_call json.dumps it at the boundary).

    Preserves execute_tool's exact gate order: filter to allowed keys ->
    _pop_database -> inject_agent_id -> validate_args -> active_database ->
    async/sync impl dispatch. When dry_run=True, runs everything up to and
    including validate_args, then returns {"dry_run": True, "ok": True} WITHOUT
    calling spec.impl (read-only by construction — no side effects).
    """
    args = dict(args or {})
    timeout = _resolve_tool_timeout(args, spec)  # pops reserved `timeout` key
    allowed_keys = set(spec.parameters.get("properties", {}).keys())
    args = {k: v for k, v in args.items() if k in allowed_keys}
    database = _pop_database(args)
    if spec.inject_agent_id and "agent_id" in allowed_keys:
        args["agent_id"] = agent_id
    args, err = validate_args(spec, args)
    if err:
        # validate_args signals failure with an "Error: …" string; surface it
        # as a structured validation error rather than a bare string.
        raise ValueError(err)
    if dry_run:
        return {"dry_run": True, "ok": True, "tool": spec.name}
    with active_database(database):
        return await _run_impl_bounded(spec, args, timeout)


# ── Dispatcher (m3_call / m3_index) ──────────────────────────────────────────
# A single generic entry point so the LLM can reach every catalog tool by name
# without each tool's JSON schema being on the MCP wire at startup. Mirrors the
# `m3 <domain> <tool>` human CLI surface; both go through execute_tool_structured
# so behavior cannot drift. See docs/DUAL_SURFACE_TOOL_ACCESS_PLAN.md.

_DESTRUCTIVE_ALLOWED = os.environ.get(
    "MCP_PROXY_ALLOW_DESTRUCTIVE", ""
).lower() in ("1", "true", "yes")

# Tools the dispatcher must NOT recurse into (would be confusing / cyclic).
_DISPATCH_EXCLUDE = frozenset({"m3_call", "m3_index", "tools_load_domain", "tools_list_domains"})


def _spec_by_name(name: str) -> ToolSpec | None:
    import mcp_tool_catalog
    for t in mcp_tool_catalog.TOOLS:
        if t.name == name:
            return t
    return None


def _did_you_mean(name: str, n: int = 3) -> list[str]:
    """Cheap prefix/substring suggestions for an unknown tool name."""
    import mcp_tool_catalog
    name = (name or "").lower()
    if not name:
        return []
    scored = []
    for t in mcp_tool_catalog.TOOLS:
        tn = t.name.lower()
        if tn.startswith(name) or name in tn:
            scored.append(t.name)
    return sorted(scored)[:n]


async def _dispatch_one(tool: str, args: dict, *, dry_run: bool) -> Any:
    """Resolve + invoke a single tool by name. Returns the native result or a
    structured error dict (never raises to the caller — batch needs per-item
    isolation)."""
    # Read _DESTRUCTIVE_ALLOWED off mcp_tool_catalog (not this module's own
    # copy): mcp_tool_catalog re-exports it as the public, monkeypatchable
    # knob (tests patch mcp_tool_catalog._DESTRUCTIVE_ALLOWED), so the gate
    # must consult that name at call time, not capture this module's value.
    import mcp_tool_catalog
    destructive_allowed = mcp_tool_catalog._DESTRUCTIVE_ALLOWED
    spec = _spec_by_name(tool)
    if spec is None:
        return {"ok": False, "error": "unknown_tool", "tool": tool,
                "did_you_mean": _did_you_mean(tool)}
    if tool in _DISPATCH_EXCLUDE:
        return {"ok": False, "error": "not_dispatchable", "tool": tool,
                "hint": "Meta/dispatcher tools cannot be called through m3_call."}
    if not destructive_allowed and not spec.default_allowed:
        return {"ok": False, "error": "destructive_gated", "tool": tool,
                "hint": "This tool mutates/deletes. Set MCP_PROXY_ALLOW_DESTRUCTIVE=1 to enable."}
    try:
        result = await execute_tool_structured(
            spec, args or {}, agent_id="", dry_run=dry_run)
        return {"ok": True, "tool": tool, "result": result}
    except Exception as e:  # validation or impl error — isolate per item
        return {"ok": False, "error": "call_failed", "tool": tool,
                "detail": f"{type(e).__name__}: {e}"}


async def m3_call_impl(tool: str = "", args: dict | None = None,
                       batch: list | None = None, dry_run: bool = False) -> str:
    """Invoke any catalog tool by name. Single: pass tool+args. Batch: pass a
    list of {tool, args} (each item isolated — one failure doesn't abort the
    rest; result order matches input). dry_run validates + checks the
    destructive gate without calling the impl. Returns JSON."""
    MAX_BATCH = 100
    if batch is not None:
        if not isinstance(batch, list):
            return json.dumps({"ok": False, "error": "bad_batch",
                               "hint": "batch must be a list of {tool, args}."})
        if len(batch) > MAX_BATCH:
            return json.dumps({"ok": False, "error": "batch_too_large",
                               "hint": f"batch capped at {MAX_BATCH} items."})
        results = []
        for item in batch:
            if not isinstance(item, dict) or "tool" not in item:
                results.append({"ok": False, "error": "bad_batch_item",
                                "hint": "each item needs a 'tool' key."})
                continue
            results.append(await _dispatch_one(
                item["tool"], item.get("args") or {},
                dry_run=bool(item.get("dry_run", dry_run))))
        return json.dumps({"batch": results}, default=str)
    if not tool:
        return json.dumps({"ok": False, "error": "missing_tool",
                           "hint": "Pass 'tool' (and optional 'args'), or 'batch'."})
    return json.dumps(await _dispatch_one(tool, args or {}, dry_run=dry_run),
                      default=str)


def _tool_arg_rows(spec: ToolSpec) -> list[dict]:
    props = spec.parameters.get("properties", {}) or {}
    required = set(spec.parameters.get("required", []))
    rows = []
    for name, pdef in props.items():
        if name == "database":  # universal injected arg — omit from the index
            continue
        rows.append({"name": name, "type": pdef.get("type", "string"),
                     "required": name in required})
    return rows


def m3_index_impl(domain: str = "") -> str:
    """List catalog tools (optionally one domain): name, domain, summary, args.
    Read-only catalog metadata — never tool output. Returns JSON."""
    import mcp_tool_catalog
    import tool_domains
    rows = []
    for t in mcp_tool_catalog.TOOLS:
        if t.name in _DISPATCH_EXCLUDE:
            continue
        d = tool_domains.domain_of_tool(t.name)
        if domain and d != domain:
            continue
        summary = (t.description or "").split(".")[0].strip()[:100]
        rows.append({"name": t.name, "domain": d, "summary": summary,
                     "destructive": not t.default_allowed,
                     "args": _tool_arg_rows(t)})
    rows.sort(key=lambda r: (r["domain"], r["name"]))
    return json.dumps({"count": len(rows), "tools": rows}, default=str)

# ── Inline impl wrapper for conversation_search ──────────────────────────────
async def _conversation_search_impl(query, k=8):
    """Search messages with automatic adjacent-turn pairing.

    When a user turn is found, the next assistant turn from the same
    conversation is included so callers always see the full Q&A pair.
    """
    ranked = await memory_core.memory_search_scored_impl(
        query, k=int(k), type_filter="message",
        extra_columns=["metadata_json", "conversation_id"],
    )
    if ranked is None:
        return "Search failed: FTS and semantic both unavailable."
    if not ranked:
        return "No results found."

    # Build initial result set
    items = []
    seen_ids: set = set()
    for score, item in ranked:
        item["score"] = score
        if "metadata_json" in item:
            item["_meta"] = json.loads(item.get("metadata_json") or "{}")
        else:
            item["_meta"] = {}
        items.append(item)
        seen_ids.add(item["id"])

    # Adjacent-turn pairing: pull the next turn for user messages
    extras = []
    for item in items:
        m = item.get("_meta", {})
        cid = item.get("conversation_id")
        if m.get("role") == "user" and "turn_index" in m and cid:
            next_idx = m["turn_index"] + 1
            with memory_core._db() as db:
                row = db.execute(
                    "SELECT id, content, title, metadata_json, conversation_id "
                    "FROM memory_items "
                    "WHERE conversation_id = ? AND is_deleted = 0 "
                    "  AND json_extract(metadata_json, '$.turn_index') = ?",
                    (cid, next_idx),
                ).fetchone()
                if row and row["id"] not in seen_ids:
                    seen_ids.add(row["id"])
                    rm = json.loads(row["metadata_json"] or "{}")
                    extras.append({
                        "id": row["id"],
                        "content": row["content"],
                        "title": row["title"],
                        "type": "message",
                        "conversation_id": row["conversation_id"],
                        "score": item["score"] * 0.85,
                        "_meta": rm,
                    })
    items.extend(extras)
    items.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Format output
    lines = [f"Top {len(items)} results:"]
    for rank, item in enumerate(items, 1):
        content = item.get("content") or ""
        lines.append("-" * 40)
        lines.append(f"{rank}. [{item['id']}] score={item['score']:.4f}  type: {item.get('type', 'unknown')}  title: {item.get('title','')}")
        lines.append(f"Content:\n{content}\n")
    lines.append("-" * 40)
    return "\n".join(lines)

# ── Inline impl wrapper for memory_verify ────────────────────────────────────
# The LLM-facing parameter is `id` (preserves the existing bridge contract);
# memory_core.memory_verify_impl uses `memory_id`. Translate here.
def _memory_verify_impl(id):
    return memory_core.memory_verify_impl(id)
