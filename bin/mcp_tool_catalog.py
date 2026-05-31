"""
mcp_tool_catalog.py — single source of truth for the m3-memory MCP tool catalog.

Imported by:
  - bin/memory_bridge.py (FastMCP stdio server — registers each spec via @mcp.tool())
  - examples/multi-agent-team/dispatch.py (orchestrator-side dispatch loop)

Zero FastMCP dependency. Pure Python + memory_core + memory_sync + memory_maintenance.
Never import this module from those modules — that would create a cycle.

Mutation-safety invariant (do not regress): mutating memory tools
(memory_delete, memory_supersede) require the FULL UUID for their target id —
a prefix is rejected via _is_full_uuid in their validators. Read tools
(memory_get) accept an 8-char prefix for convenience, but an ambiguous prefix
on a mutation could close/delete the wrong memory irreversibly. This asymmetry
is intentional; keep the validators and the "full UUID required" wording in the
tool descriptions so it survives doc-inventory regeneration. Also note:
memory_supersede is non-destructive and creates a NEW successor each call — it
is an update primitive, not a delete; do not chain it to "clean up" clutter.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable

import importlib

class LazyImpl:
    def __init__(self, module_name: str, attr_name: str):
        self.module_name = module_name
        self.attr_name = attr_name
        self._cached_func = None

    def __call__(self, *args, **kwargs):
        if self._cached_func is None:
            mod = importlib.import_module(self.module_name)
            self._cached_func = getattr(mod, self.attr_name)
        return self._cached_func(*args, **kwargs)

class LazyModuleProxy:
    def __init__(self, module_name: str):
        self._module_name = module_name

    def __getattr__(self, name: str):
        return LazyImpl(self._module_name, name)

chatlog_core = LazyModuleProxy("chatlog_core")
chatlog_status = LazyModuleProxy("chatlog_status")
memory_core = LazyModuleProxy("memory_core")
memory_maintenance = LazyModuleProxy("memory_maintenance")
memory_sync = LazyModuleProxy("memory_sync")
_files_tools = LazyModuleProxy("files_memory.tools")

from m3_sdk import active_database

# ── Validation Constants (hoisted from memory_bridge.py) ─────────────────────
MAX_CONTENT_SIZE = 50_000
MAX_QUERY_LENGTH = 2_000
MAX_K = 100
VALID_MEMORY_TYPES = frozenset({
    "note", "fact", "decision", "preference", "conversation", "message",
    "task", "code", "config", "observation", "plan", "summary", "snippet",
    "reference", "log", "home", "user_fact", "scratchpad", "auto",
    "knowledge", "event_extraction", "fact_enriched", "chat_log",
    # Home-network / infrastructure inventory categories. Pre-existing rows
    # in the store predate the strict catalog (restored 2026-04-17 from the
    # pre-hard-delete archive); widening lets new writes round-trip cleanly.
    "local_device", "network_config", "infrastructure", "home_automation",
    "migration-log",
    # Security: SSH endpoints, credentials references, firewall rules,
    # auth-related facts. Distinct from generic 'fact' for browsing.
    "security",
    # Platform-scoped notes — for guidance/snippets that only apply on one OS.
    "windows_only", "macos_only", "linux_only",
    # User-facing reminder / pending action. Lighter than 'task' which carries
    # the full task-state machine; 'to_do' is just "remember to do this".
    "to_do",
})

# Entity-graph enums — defined in memory_core to avoid circular import
# (mcp_tool_catalog imports memory_core, not vice versa). Re-exported here
# so callers see a single import surface.
VALID_ENTITY_TYPES = memory_core.VALID_ENTITY_TYPES
VALID_ENTITY_PREDICATES = memory_core.VALID_ENTITY_PREDICATES

# Canonical UUID shape (8-4-4-4-12 hex). Mutating tools (supersede/delete)
# require a full UUID, not the 8-char prefix that memory_get accepts for reads
# — an ambiguous prefix on a mutation could close the wrong memory.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _is_full_uuid(s: str) -> bool:
    """True iff s is a full canonical UUID (not a prefix). Used to gate
    mutating memory tools against prefix-based id ambiguity."""
    return bool(_UUID_RE.match(s.strip()))

# ── Dataclass ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict
    impl: Callable
    is_async: bool = False
    validators: tuple = ()
    default_allowed: bool = True
    inject_agent_id: bool = False

# ── Validators ───────────────────────────────────────────────────────────────
def _memory_write_validator(args: dict) -> Any:
    t = args.get("type", "")
    if t not in VALID_MEMORY_TYPES:
        return f"Error: invalid memory type '{t}'. Valid types: {', '.join(sorted(VALID_MEMORY_TYPES))}"
    content = args.get("content", "") or ""
    if content and len(content) > MAX_CONTENT_SIZE:
        return f"Error: content too large ({len(content)} chars). Maximum is {MAX_CONTENT_SIZE}."
    md = args.get("metadata", "{}")
    if isinstance(md, dict):
        args["metadata"] = json.dumps(md)
    elif isinstance(md, str) and md and md != "{}":
        try:
            json.loads(md)
        except (ValueError, json.JSONDecodeError):
            return "Error: metadata is not valid JSON."
    if t == "auto":
        args["auto_classify"] = True
    return args

def _memory_delete_validator(args: dict) -> Any:
    """Validator for memory_delete. Requires a full UUID, never a prefix —
    a prefix could delete the wrong memory irreversibly (full UUID required
    for mutation safety). memory_get accepts a prefix for reads; this does not."""
    mid = args.get("id", "")
    if not mid or not str(mid).strip():
        return "Error: id is required (the full UUID of the memory to delete)."
    mid = str(mid).strip()
    if not _is_full_uuid(mid):
        return ("Error: id must be the full UUID, not a prefix — "
                "full UUID required for mutation safety. "
                "Resolve the prefix first with memory_get (which accepts a "
                "prefix), then pass the returned full id.")
    args["id"] = mid
    return args

def _memory_supersede_validator(args: dict) -> Any:
    """Validator for memory_supersede. Like _memory_write_validator but `type`
    is optional — an empty type means "inherit the old memory's type" (see
    memory_supersede_impl). `old_id` must be a non-empty string."""
    old_id = args.get("old_id", "")
    if not old_id or not str(old_id).strip():
        return "Error: old_id is required (the id of the memory to supersede)."
    old_id = str(old_id).strip()
    # Mutation safety: require a full UUID, never an id prefix. memory_get
    # accepts an 8-char prefix for reads, but an ambiguous prefix on a
    # mutation could close the wrong memory irreversibly, so supersede/delete
    # demand the exact full id. See _is_full_uuid.
    if not _is_full_uuid(old_id):
        return ("Error: old_id must be the full UUID, not a prefix — "
                "full UUID required for mutation safety. "
                "Resolve the prefix first with memory_get (which accepts a "
                "prefix), then pass the returned full id.")
    args["old_id"] = old_id
    t = args.get("type", "")
    # Empty type is the inherit-from-old sentinel; only validate a non-empty one.
    if t and t not in VALID_MEMORY_TYPES:
        return f"Error: invalid memory type '{t}'. Valid types: {', '.join(sorted(VALID_MEMORY_TYPES))}"
    content = args.get("content", "") or ""
    if content and len(content) > MAX_CONTENT_SIZE:
        return f"Error: content too large ({len(content)} chars). Maximum is {MAX_CONTENT_SIZE}."
    md = args.get("metadata", "{}")
    if isinstance(md, dict):
        args["metadata"] = json.dumps(md)
    elif isinstance(md, str) and md and md != "{}":
        try:
            json.loads(md)
        except (ValueError, json.JSONDecodeError):
            return "Error: metadata is not valid JSON."
    return args

def _memory_search_validator(args: dict) -> Any:
    q = args.get("query", "")
    if not q or not str(q).strip():
        return "Error: query cannot be empty."
    q = str(q)
    if len(q) > MAX_QUERY_LENGTH:
        q = q[:MAX_QUERY_LENGTH]
    args["query"] = q
    try:
        k = int(args.get("k", 8))
    except (TypeError, ValueError):
        k = 8
    args["k"] = max(1, min(k, MAX_K))
    return args

def _variant_gate(args: dict) -> dict:
    """Bench-data gate for memory_search / memory_suggest. If include_bench_data=True,
    drop the variant filter; otherwise default to '__none__' so bench rows hide."""
    include_bench = bool(args.pop("include_bench_data", False))
    if include_bench:
        args["variant"] = ""
    elif not args.get("variant"):
        args["variant"] = "__none__"
    return args

def _memory_search_gated_validator(args: dict) -> Any:
    r = _memory_search_validator(args)
    if isinstance(r, str):
        return r
    return _variant_gate(r)

def _memory_suggest_validator(args: dict) -> Any:
    return _variant_gate(args)

def _memory_search_scored_validator(args: dict) -> Any:
    """Validator for memory_search_scored.

    Deliberately does NOT reuse `_memory_search_gated_validator`: that path's
    `_memory_search_validator` rejects an empty query, but the scored search is
    the structured backend for filter-only listings (e.g. a memory-provider's
    get_all/profile call passes query='' + type_filter). So this validator
    clamps query length and k, runs the SAME bench-data gate as memory_search
    (`_variant_gate`, defaulting variant -> '__none__' so bench rows stay
    hidden — keeps the scored sibling's row set identical to memory_search's),
    but allows an empty query through.
    """
    q = str(args.get("query", ""))
    if len(q) > MAX_QUERY_LENGTH:
        q = q[:MAX_QUERY_LENGTH]
    args["query"] = q
    try:
        k = int(args.get("k", 8))
    except (TypeError, ValueError):
        k = 8
    args["k"] = max(1, min(k, MAX_K))
    return _variant_gate(args)

def _memory_update_validator(args: dict) -> Any:
    md = args.get("metadata", "")
    if isinstance(md, dict):
        args["metadata"] = json.dumps(md)
    return args

def _memory_set_retention_validator(args: dict) -> Any:
    try:
        args["max_memories"] = int(args.get("max_memories", 1000))
        args["ttl_days"]     = int(args.get("ttl_days", 0))
        args["auto_archive"] = int(args.get("auto_archive", 1))
    except (TypeError, ValueError):
        return "Error: max_memories, ttl_days, and auto_archive must be integers."
    return args

def _gdpr_user_id_validator(args: dict) -> Any:
    uid = args.get("user_id", "")
    if not uid or not str(uid).strip():
        return "Error: user_id is required."
    args["user_id"] = str(uid).strip()
    return args

# ── Helpers ──────────────────────────────────────────────────────────────────
def get_tool(name: str) -> ToolSpec | None:
    return _BY_NAME.get(name)

def default_allowlist() -> set[str]:
    return {t.name for t in TOOLS if t.default_allowed}

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

async def execute_tool(spec: ToolSpec, args: dict, agent_id: str) -> str:
    try:
        allowed_keys = set(spec.parameters.get("properties", {}).keys())
        args = {k: v for k, v in (args or {}).items() if k in allowed_keys}
        database = _pop_database(args)
        if spec.inject_agent_id and "agent_id" in allowed_keys:
            args["agent_id"] = agent_id
        args, err = validate_args(spec, args)
        if err:
            return err
        with active_database(database):
            if spec.is_async:
                result = await spec.impl(**args)
            else:
                result = spec.impl(**args)
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
    allowed_keys = set(spec.parameters.get("properties", {}).keys())
    args = {k: v for k, v in (args or {}).items() if k in allowed_keys}
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
        if spec.is_async:
            return await spec.impl(**args)
        return spec.impl(**args)


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
    for t in TOOLS:
        if t.name == name:
            return t
    return None


def _did_you_mean(name: str, n: int = 3) -> list[str]:
    """Cheap prefix/substring suggestions for an unknown tool name."""
    name = (name or "").lower()
    if not name:
        return []
    scored = []
    for t in TOOLS:
        tn = t.name.lower()
        if tn.startswith(name) or name in tn:
            scored.append(t.name)
    return sorted(scored)[:n]


async def _dispatch_one(tool: str, args: dict, *, dry_run: bool) -> Any:
    """Resolve + invoke a single tool by name. Returns the native result or a
    structured error dict (never raises to the caller — batch needs per-item
    isolation)."""
    spec = _spec_by_name(tool)
    if spec is None:
        return {"ok": False, "error": "unknown_tool", "tool": tool,
                "did_you_mean": _did_you_mean(tool)}
    if tool in _DISPATCH_EXCLUDE:
        return {"ok": False, "error": "not_dispatchable", "tool": tool,
                "hint": "Meta/dispatcher tools cannot be called through m3_call."}
    if not _DESTRUCTIVE_ALLOWED and not spec.default_allowed:
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
    import tool_domains
    rows = []
    for t in TOOLS:
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

import tool_loader as _tool_loader  # provides lazy domain-expansion impls

# ── TOOLS catalog ────────────────────────────────────────────────────────────
TOOLS: list[ToolSpec] = [
    # ── Meta-tools: lazy domain loading ──────────────────────────────────────
    # These two ALWAYS register at MCP startup. Every other tool may be hidden
    # behind a domain — the agent calls `tools_load_domain` to expose them.
    # Set M3_TOOLS_LAZY=0 to opt out and expose all tools eagerly.
    ToolSpec(
        name="tools_list_domains",
        description=(
            "List m3 tool domains (memory, chatlog, files, entity, agent, tasks, "
            "conversations, admin) and their tool counts. Call `tools_load_domain` "
            "to expose a domain's full tool surface."
        ),
        parameters={"type": "object", "properties": {}, "required": []},
        impl=_tool_loader.list_domains,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="tools_load_domain",
        description=(
            "Register a tool domain's full surface for the current MCP session. "
            "Use when you need tools beyond the essentials (memory_search, "
            "memory_write, memory_get, chatlog_search, chatlog_write, files_search). "
            "Valid domains: memory, chatlog, files, entity, agent, tasks, "
            "conversations, admin."
        ),
        parameters={
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Domain to expose. See `tools_list_domains`.",
                },
            },
            "required": ["domain"],
        },
        impl=_tool_loader.load_domain,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="m3_help_capabilities",
        description=(
            "Discover m3-memory tool capabilities, parameters, and availability. "
            "Allows filtering by a logical domain (memory, chatlog, files, entity, "
            "agent, tasks, conversations, admin, diagnostics) or searching by keywords."
        ),
        parameters={
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Optional domain to filter capabilities (e.g., 'memory', 'files').",
                },
                "query": {
                    "type": "string",
                    "description": "Optional keyword search term to filter tools.",
                },
            },
            "required": [],
        },
        impl=_tool_loader.help_capabilities,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="m3_index",
        description=(
            "List m3 catalog tools (optionally one domain) as structured rows: "
            "name, domain, one-line summary, destructive flag, and arg specs "
            "(name/type/required). Use this to discover the exact args for any "
            "tool before calling it via m3_call — cheaper than a failed call. "
            "Read-only catalog metadata; never returns tool output. Domains: "
            "memory, chatlog, files, entity, agent, tasks, conversations, admin."
        ),
        parameters={
            "type": "object",
            "properties": {
                "domain": {
                    "type": "string",
                    "description": "Filter to one domain (empty = whole catalog).",
                    "default": "",
                },
            },
            "required": [],
        },
        impl=m3_index_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="m3_call",
        description=(
            "Invoke ANY m3 catalog tool by name without loading its domain — the "
            "low-token path to the full tool surface. Single call: pass `tool` "
            "(e.g. 'files_stats') and `args` (an object). Batch: pass `batch`, a "
            "list of {tool, args} (each isolated — one failure won't abort the "
            "rest; capped at 100). Set `dry_run` to validate args + check the "
            "destructive gate WITHOUT executing. Returns JSON. Call `m3_index` "
            "first if you don't know a tool's args. Destructive tools require "
            "MCP_PROXY_ALLOW_DESTRUCTIVE=1."
        ),
        parameters={
            "type": "object",
            "properties": {
                "tool":    {"type": "string", "description": "Catalog tool name (see m3_index).", "default": ""},
                "args":    {"type": "object", "description": "Arguments object for the target tool.", "default": {}},
                "batch":   {"type": "array", "description": "List of {tool, args} for one-round-trip batch dispatch.", "default": None},
                "dry_run": {"type": "boolean", "description": "Validate + gate-check only; do not execute.", "default": False},
            },
            "required": [],
        },
        impl=m3_call_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_write",
        description=(
            "Creates a MemoryItem and optionally embeds it for semantic search. "
            "Contradiction detection is automatic — if new content conflicts with an existing "
            "memory of the same type/title, the old one is superseded. "
            "Use type='auto' to let the LLM decide the best category."
        ),
        parameters={
            "type": "object",
            "properties": {
                "type":          {"type": "string", "description": f"Memory type. One of: {', '.join(sorted(VALID_MEMORY_TYPES))}."},
                "content":       {"type": "string", "description": "Memory body (max 50000 chars)."},
                "title":         {"type": "string", "description": "Short title.", "default": ""},
                "metadata":      {"type": "string", "description": "JSON-encoded metadata object.", "default": "{}"},
                "agent_id":      {"type": "string", "description": "Owning agent id. Injected by the orchestrator.", "default": ""},
                "model_id":      {"type": "string", "description": "Originating model id.", "default": ""},
                "change_agent":  {"type": "string", "description": "Agent causing the write (audit).", "default": ""},
                "importance":    {"type": "number", "description": "0.0-1.0 relevance.", "default": 0.5},
                "source":        {"type": "string", "description": "Provenance tag.", "default": "agent"},
                "embed":         {"type": "boolean", "description": "Embed for semantic search.", "default": True},
                "user_id":       {"type": "string", "description": "Data subject id.", "default": ""},
                "scope":         {"type": "string", "description": "Isolation scope.", "default": "agent"},
                "valid_from":    {"type": "string", "description": "ISO-8601 validity start.", "default": ""},
                "valid_to":      {"type": "string", "description": "ISO-8601 validity end.", "default": ""},
                "auto_classify": {"type": "boolean", "description": "Let the LLM pick the type (forced true if type='auto').", "default": False},
                "conversation_id": {"type": "string", "description": "Groups this memory with a conversation / team session. Same ID space as conversation_start.", "default": ""},
                "refresh_on":    {"type": "string", "description": "ISO-8601 timestamp when this memory should be flagged for review (lifecycle / planned obsolescence).", "default": ""},
                "refresh_reason": {"type": "string", "description": "Why this memory needs refreshing (e.g., 'quarterly policy review').", "default": ""},
                "variant":       {"type": "string", "description": "Pipeline identifier for A/B variant tracking.", "default": ""},
                "embed_text":    {"type": "string", "description": "Override text used for embedding; falls back to content when empty.", "default": ""},
            },
            "required": ["type", "content"],
        },
        impl=memory_core.memory_write_impl,
        is_async=True,
        validators=(_memory_write_validator,),
        default_allowed=True,
        inject_agent_id=True,
    ),
    ToolSpec(
        name="memory_write_from_file",
        description=(
            "Write a memory whose content is read from a file on disk. Use this "
            "when the memory body is large (>1k chars) to avoid the autoregressive "
            "decode latency of streaming a multi-thousand-token JSON `input` field "
            "through tool_use — write the body with the Write tool first (off the "
            "streaming path, fast), then call this tool with just the path + tiny "
            "metadata. The MCP server reads the file, writes the row through the "
            "same path as memory_write (all gates apply), and by default deletes "
            "the source file on success. Path must be absolute on the host running "
            "this MCP server. Files >200000 bytes are rejected; underlying content "
            "is still capped at 50000 chars by memory_write_impl."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path":          {"type": "string", "description": "Absolute path to a UTF-8 text file on the MCP server host. The file's contents become the memory `content`."},
                "type":          {"type": "string", "description": f"Memory type. One of: {', '.join(sorted(VALID_MEMORY_TYPES))}."},
                "title":         {"type": "string", "description": "Short title.", "default": ""},
                "metadata":      {"type": "string", "description": "JSON-encoded metadata object.", "default": "{}"},
                "agent_id":      {"type": "string", "description": "Owning agent id. Injected by the orchestrator.", "default": ""},
                "model_id":      {"type": "string", "description": "Originating model id.", "default": ""},
                "change_agent":  {"type": "string", "description": "Agent causing the write (audit).", "default": ""},
                "importance":    {"type": "number", "description": "0.0-1.0 relevance.", "default": 0.5},
                "source":        {"type": "string", "description": "Provenance tag.", "default": "agent"},
                "embed":         {"type": "boolean", "description": "Embed for semantic search.", "default": True},
                "user_id":       {"type": "string", "description": "Data subject id.", "default": ""},
                "scope":         {"type": "string", "description": "Isolation scope.", "default": "agent"},
                "valid_from":    {"type": "string", "description": "ISO-8601 validity start.", "default": ""},
                "valid_to":      {"type": "string", "description": "ISO-8601 validity end.", "default": ""},
                "auto_classify": {"type": "boolean", "description": "Let the LLM pick the type (forced true if type='auto').", "default": False},
                "conversation_id": {"type": "string", "description": "Groups this memory with a conversation / team session.", "default": ""},
                "refresh_on":    {"type": "string", "description": "ISO-8601 timestamp when this memory should be flagged for review.", "default": ""},
                "refresh_reason": {"type": "string", "description": "Why this memory needs refreshing.", "default": ""},
                "variant":       {"type": "string", "description": "Pipeline identifier for A/B variant tracking.", "default": ""},
                "delete_after_read": {"type": "boolean", "description": "Delete the source file after successful write. Default true (signals contents are now authoritative in m3-memory).", "default": True},
            },
            "required": ["path", "type"],
        },
        impl=memory_core.memory_write_from_file_impl,
        is_async=True,
        validators=(_memory_write_validator,),
        default_allowed=True,
        inject_agent_id=True,
    ),
    ToolSpec(
        name="memory_supersede",
        description=(
            "Explicitly supersede an existing memory with a new one. Use this to "
            "record an intentional update — 'this fact replaces that specific "
            "memory' — when you know the old memory's id. Unlike memory_write's "
            "automatic contradiction detection (a cosine + title heuristic that "
            "may link the wrong prior memory or none at all), this targets the "
            "given old_id deterministically. Non-destructive: the old memory is "
            "retained, its validity interval is closed (is_deleted=1, valid_to "
            "set), and a 'supersedes' edge is recorded new -> old. The old "
            "memory stays retrievable by id and via memory_history, and "
            "as_of-filtered search still sees it valid before the supersession "
            "point — it is only dropped from default search. Fields you omit "
            "(type, title, importance, scope) are inherited from the old memory, "
            "so pass only what changed. To hard-delete instead, that is a "
            "separate gated tool (memory_delete). "
            "old_id MUST be the full UUID — a prefix is rejected (full UUID "
            "required for mutation safety; memory_get accepts a prefix, this "
            "does not). Note: each supersede creates a NEW successor memory; "
            "call it once with the full id, do not chain supersedes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "old_id":       {"type": "string", "description": "FULL UUID of the memory being superseded (not an 8-char prefix — a prefix is rejected for mutation safety). Must exist and not already be deleted/superseded."},
                "content":      {"type": "string", "description": "Body of the replacement memory (max 50000 chars)."},
                "type":         {"type": "string", "description": f"Memory type of the replacement. Omit to inherit the old memory's type. One of: {', '.join(sorted(VALID_MEMORY_TYPES))}.", "default": ""},
                "title":        {"type": "string", "description": "Title of the replacement. Omit to inherit the old memory's title.", "default": ""},
                "metadata":     {"type": "string", "description": "JSON-encoded metadata object for the replacement.", "default": "{}"},
                "agent_id":     {"type": "string", "description": "Owning agent id. Injected by the orchestrator.", "default": ""},
                "model_id":     {"type": "string", "description": "Originating model id.", "default": ""},
                "change_agent": {"type": "string", "description": "Agent causing the supersede (audit).", "default": ""},
                "importance":   {"type": "number", "description": "0.0-1.0 relevance of the replacement. Omit (leave at -1) to inherit the old memory's importance.", "default": -1.0},
                "source":       {"type": "string", "description": "Provenance tag.", "default": "agent"},
                "embed":        {"type": "boolean", "description": "Embed the replacement for semantic search.", "default": True},
                "user_id":      {"type": "string", "description": "Data subject id.", "default": ""},
                "scope":        {"type": "string", "description": "Isolation scope of the replacement. Omit to inherit the old memory's scope.", "default": ""},
                "valid_from":   {"type": "string", "description": "ISO-8601 validity start of the replacement; also the point at which the old memory's validity is closed. Defaults to now.", "default": ""},
                "variant":      {"type": "string", "description": "Pipeline identifier for A/B variant tracking.", "default": ""},
                "embed_text":   {"type": "string", "description": "Override text used for embedding; falls back to content when empty.", "default": ""},
            },
            "required": ["old_id", "content"],
        },
        impl=memory_core.memory_supersede_impl,
        is_async=True,
        validators=(_memory_supersede_validator,),
        default_allowed=True,
        inject_agent_id=True,
    ),
    ToolSpec(
        name="memory_search",
        description="Search across memory items using semantic similarity or keyword matching. Filter by user_id and scope for isolation.",
        parameters={
            "type": "object",
            "properties": {
                "query":              {"type": "string", "description": "Search query."},
                "k":                  {"type": "integer", "description": "Max results (1-100).", "default": 8},
                "type_filter":        {"type": "string", "description": "Restrict to a memory type.", "default": ""},
                "agent_filter":       {"type": "string", "description": "Restrict to an agent id.", "default": ""},
                "search_mode":        {"type": "string", "enum": ["hybrid", "semantic", "keyword"], "description": "Retrieval mode.", "default": "hybrid"},
                "include_scratchpad": {"type": "boolean", "description": "Include ephemeral scratchpad items.", "default": False},
                "user_id":            {"type": "string", "description": "Filter by data subject.", "default": ""},
                "scope":              {"type": "string", "description": "Filter by isolation scope.", "default": ""},
                "as_of":              {"type": "string", "description": "ISO-8601 time-travel cutoff.", "default": ""},
                "conversation_id":    {"type": "string", "description": "Restrict to a conversation / team session.", "default": ""},
                "recency_bias":       {"type": "number", "description": "Boost newer items (0.0=off, 0.1-0.2=moderate, higher=aggressive). Useful for 'current' or 'latest' queries.", "default": 0.0},
                "adaptive_k":         {"type": "boolean", "description": "Auto-trim results at the score drop-off point. WARNING: regresses on temporal-reasoning, knowledge-update, and multi-session queries; safe only for sharp-curve queries where most retrievals would be noise. Prefer `auto_route=True` on memory_search_routed for safer multi-signal routing.", "default": False},
                "variant":            {"type": "string", "description": "Ingest-pipeline filter. '' = real user data only (default, equivalent to IS NULL). Pass a specific variant name (e.g. 'heuristic_c1c4') to scope to that bench ingest.", "default": ""},
                "include_bench_data": {"type": "boolean", "description": "Opt in to LOCOMO / LongMemEval bench rows. Default False hides any row with a variant tag.", "default": False},
            },
            "required": ["query"],
        },
        impl=memory_core.memory_search_impl,
        is_async=True,
        validators=(_memory_search_gated_validator,),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_search_scored",
        description=(
            "Structured hybrid FTS5+vector+MMR search. Returns ranked rows "
            "[(score, item)] with content + metadata (id, valid_from, "
            "conversation_id, user_id) — NOT formatted text. Use when a caller "
            "needs parseable rows rather than an LLM-readable block (e.g. a "
            "memory-provider backend). Empty query + type_filter = filter-only "
            "listing. Same bench-data gate as memory_search."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query":              {"type": "string", "description": "Search text. Empty string = filter-only (type/scope) listing.", "default": ""},
                "k":                  {"type": "integer", "description": "Max rows (1-100).", "default": 8},
                "type_filter":        {"type": "string", "description": "Restrict to a memory type (e.g. 'user_fact').", "default": ""},
                "agent_filter":       {"type": "string", "description": "Restrict to an agent id.", "default": ""},
                "search_mode":        {"type": "string", "enum": ["hybrid", "semantic", "keyword"], "description": "Retrieval mode.", "default": "hybrid"},
                "user_id":            {"type": "string", "description": "Filter by data subject.", "default": ""},
                "scope":              {"type": "string", "description": "Filter by isolation scope.", "default": ""},
                "as_of":              {"type": "string", "description": "ISO-8601 time-travel cutoff (bitemporal point-in-time query).", "default": ""},
                "conversation_id":    {"type": "string", "description": "Restrict to a conversation / team session.", "default": ""},
                "recency_bias":       {"type": "number", "description": "Boost newer items (0.0=off).", "default": 0.0},
                "variant":            {"type": "string", "description": "Ingest-pipeline filter. '' = real user data only.", "default": ""},
                "include_bench_data": {"type": "boolean", "description": "Opt in to LOCOMO / LongMemEval bench rows. Default False hides any variant-tagged row.", "default": False},
            },
            "required": [],
        },
        impl=memory_core.memory_search_scored_impl,
        is_async=True,
        validators=(_memory_search_scored_validator,),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_suggest",
        description="Preview which memories would be retrieved for a query, with score breakdowns explaining why each was selected.",
        parameters={
            "type": "object",
            "properties": {
                "query":              {"type": "string", "description": "Search query."},
                "k":                  {"type": "integer", "description": "Max results to preview.", "default": 5},
                "variant":            {"type": "string", "description": "Ingest-pipeline filter. Default '__none__' = real user data only.", "default": "__none__"},
                "include_bench_data": {"type": "boolean", "description": "Opt in to bench rows. Default False.", "default": False},
            },
            "required": ["query"],
        },
        impl=memory_core.memory_suggest_impl,
        is_async=True,
        validators=(_memory_suggest_validator,),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_search_routed",
        description="Temporal-aware routed retrieval. Routes temporal queries to verbatim search at k+temporal_k_bump; non-temporal queries to (optionally fact-fused) max-kind search at k. Pass fact_variant for two-tier fact-fusion. Optional graph_depth and expand_sessions add post-retrieval neighbor expansion.",
        parameters={
            "type": "object",
            "properties": {
                "query":           {"type": "string", "description": "Search query."},
                "k":               {"type": "integer", "default": 10, "description": "Top-K to return."},
                "fact_variant":    {"type": "string", "default": "", "description": "Optional fact-tier variant to fuse with base. Empty = single-variant."},
                "temporal_k_bump": {"type": "integer", "default": 5, "description": "Extra slots added when query is temporal."},
                "graph_depth":     {"type": "integer", "default": 0, "description": "If > 0, traverse memory_relationships up to N hops from each top-K hit and re-fuse. Clamped to 3."},
                "expand_sessions": {"type": "boolean", "default": False, "description": "If true, pull all turns sharing each top-K hit's conversation_id (capped at session_cap) and re-fuse."},
                "session_cap":     {"type": "integer", "default": 12, "description": "Per-session turn cap when expand_sessions=true."},
                "user_id":         {"type": "string", "default": ""},
                "scope":           {"type": "string", "default": ""},
                "type_filter":     {"type": "string", "default": ""},
                "agent_filter":    {"type": "string", "default": ""},
                "search_mode":     {"type": "string", "default": "hybrid"},
                "variant":         {"type": "string", "default": ""},
                "as_of":           {"type": "string", "default": ""},
                "conversation_id": {"type": "string", "default": ""},
                "explain":         {"type": "boolean", "default": False},
                "entity_graph":    {"type": "boolean", "description": "Direct lever for entity-graph expansion. Default False = OFF (production default; matches memory_search behavior). True = parse query for named entities, traverse entity_relationships up to entity_graph_depth hops, fold matched memory_ids into the result set tagged expanded_via='entity_graph'. The AUTO layer can also flip this on for the entity_anchored branch (see auto_entity_graph_enabled), but caller-explicit entity_graph=True works without auto_route. Use for benchmarking + production opt-in once empirically validated.", "default": False},
                "rerank":          {"type": "boolean", "description": "Cross-encoder reranking. Default False = OFF (production behavior unchanged). True = re-score top (rerank_pool_k or 3*k) hits with sentence-transformers CrossEncoder, blend with hybrid score per rerank_blend, re-sort. Adds ~12MB-560MB model download (cached at ~/.cache/torch) + ~50ms/pair on GPU, ~200ms/pair on CPU. Used in benchmarking; opt-in for production retrieval.", "default": False},
                "rerank_model":    {"type": "string", "description": "Cross-encoder model id. Default empty = DEFAULT_RERANK_MODEL (cross-encoder/ms-marco-MiniLM-L-6-v2 — small, fast, English-tuned). Higher-accuracy alternative: BAAI/bge-reranker-v2-m3 (multilingual, larger, slower). Override via M3_RERANK_MODEL env var.", "default": ""},
                "rerank_pool_k":   {"type": "integer", "description": "Pool size before rerank. Default 0 = 3*k. Higher pool = more candidates rescored = slower but potentially higher recall. Never truncates below final k. Only used when rerank=True.", "default": 0},
                "rerank_blend":    {"type": "number", "description": "Blend factor: final_score = rerank_blend * ce_score + (1 - rerank_blend) * hybrid_score. Default 1.0 = pure CE replacement (most aggressive). 0.5 = average. 0.3 = CE as tiebreaker over hybrid. 0.0 = no-op (skip rerank — same effect as rerank=False). Only used when rerank=True.", "default": 1.0},
                "entity_graph_depth":         {"type": "integer", "description": "BFS hop count over entity_relationships when entity_graph=True. Default 1 = direct neighbors only. Clamped to [1,3] core-side. Higher depth pulls more neighbors but adds noise; depth=2 typically helps multi-hop questions but regresses sharp single-fact lookups.", "default": 1},
                "entity_graph_max_neighbors": {"type": "integer", "description": "Cap on entity nodes discovered during BFS traversal when entity_graph=True. Default 20. Clamped to [1,100] core-side. Lower = fewer rows folded in but tighter relevance; higher = broader recall at cost of precision.", "default": 20},
                "auto_route":      {"type": "boolean", "description": "Multi-signal retrieval routing. Default False = no auto-routing (existing behavior). True = router picks branch (temporal/multi_session/sharp/default) and fills in unset parameters with branch-specific values; caller-explicit values still win.", "default": False},
                "auto_top1_sharp_min": {"type": "number", "description": "Top-1 score above which query is marked sharp. Default 0.89. Used by sharp branch to detect high-confidence queries.", "default": 0.89},
                "auto_slope_at_3_sharp_min": {"type": "number", "description": "Slope-at-3 (score drop per rank) above which query is marked sharp. Default 0.08. Steeper curves = more relevance discrimination.", "default": 0.08},
                "auto_conv_id_diversity_threshold": {"type": "integer", "description": "Number of distinct conversation IDs in top-10 hits above which query is routed to multi_session. Default 5. Higher threshold = only very scattered results trigger expansion.", "default": 5},
                "auto_top1_low_threshold": {"type": "number", "description": "Score floor for sharp detection (OOD guard). Default 0.50. Below this, query is not marked sharp even if other signals fire (prevents misclassifying low-confidence matches).", "default": 0.50},
                "auto_temporal_k": {"type": "integer", "description": "k for temporal branch when auto_route=True. Default 15. Branch fires when query has temporal cues (when/since/before/after/dates).", "default": 15},
                "auto_temporal_recency_bias": {"type": "number", "description": "recency_bias for temporal branch. Default 0.05. Boosts recent memories over older ones (useful for 'recently'/'today' questions).", "default": 0.05},
                "auto_temporal_expand_sessions": {"type": "boolean", "description": "expand_sessions for temporal branch. Default True. Pulls full conversation context when a temporal hit is found (important for 'what happened after X' questions).", "default": True},
                "auto_temporal_graph_depth": {"type": "integer", "description": "graph_depth for temporal branch (AUTO_v2 fix). Default 1. Traverses memory relationships to find cross-temporal references (e.g., follow-up discussions on an earlier event).", "default": 1},
                "auto_multi_k": {"type": "integer", "description": "k for multi_session branch when auto_route=True. Default 20. Branch fires on comparison queries (how many/count/total) or when hits scatter across multiple conversations.", "default": 20},
                "auto_multi_expand_sessions": {"type": "boolean", "description": "expand_sessions for multi_session branch. Default True. Pulls all turns from detected conversation IDs for aggregate comparisons ('list all X across conversations').", "default": True},
                "auto_sharp_threshold_ratio": {"type": "number", "description": "Trim hits below (top_score * ratio) in sharp branch. Default 0.85. Removes tail noise when there's a clear high-confidence cluster.", "default": 0.85},
                "auto_sharp_k_min": {"type": "integer", "description": "Floor for hit count after sharp threshold trim. Default 3. Ensures at least this many hits even if threshold trim is aggressive.", "default": 3},
                "auto_sharp_k_max": {"type": "integer", "description": "Ceiling for hit count after sharp threshold trim. Default 10. Caps result set when sharp curve is very steep.", "default": 10},
                "auto_entity_graph_enabled": {"type": "boolean", "description": "AUTO entity-anchored branch enable. Default True = branch fires when query has named entities AND auto_route=True. Caller can pass False to disable AUTO from enabling entity_graph (still works if caller passes entity_graph=True explicitly).", "default": True},
                "auto_entity_graph_depth": {"type": "integer", "description": "Entity-graph traversal depth when AUTO entity_anchored branch fires. Default 1 = single-hop neighbors. Higher (2-3) traverses further but adds noise.", "default": 1},
                "auto_entity_graph_max_neighbors": {"type": "integer", "description": "Cap on entities expanded during AUTO entity-anchored traversal. Default 20.", "default": 20},
                "auto_entity_graph_named_entity_threshold": {"type": "integer", "description": "Minimum named-entity count in query for AUTO entity_anchored branch to fire. Default 1 = fire on any proper noun phrase (two+ capitalized words). Higher (2-3) is more conservative.", "default": 1},
                "entity_graph_valid_types": {"type": "array", "items": {"type": "string"}, "description": "Override list of allowed entity_type values for graph traversal. Default empty = use core defaults (person, place, organization, event, concept, object, date). Pass a list to filter traversal to specific types.", "default": []},
                "entity_graph_valid_predicates": {"type": "array", "items": {"type": "string"}, "description": "Override list of allowed predicate values for graph traversal. Default empty = use core defaults (works_at, located_in, before, after, same_as, contradicts, mentions, relates_to). Pass a list to filter traversal to specific predicates.", "default": []},
            },
            "required": ["query"],
        },
        impl=memory_core.memory_search_routed_impl,
        is_async=True,
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_search_multi_db",
        description="Search across multiple SQLite databases (e.g. agent_memory.db AND agent_chatlog.db) in one call. Each DB is searched independently via hybrid FTS5+vector search and the top results are merged by score. Returns the global top-K with each item tagged with its source database. Caveat: FTS5 BM25 scores depend on per-DB corpus stats so cross-DB ranks are approximate; works well for small fan-out (typically 2-5 DBs sharing the same embed_model).",
        parameters={
            "type": "object",
            "properties": {
                "query":           {"type": "string", "description": "Search query."},
                "databases":       {"type": "string", "description": "Comma-separated list of DB paths to fan out to (e.g. 'memory/agent_memory.db,memory/agent_chatlog.db'). Empty = no-op."},
                "k":               {"type": "integer", "default": 8, "description": "Top-K to return globally after merge. Each per-DB search also retrieves K, then results are pooled and re-sorted."},
                "type_filter":     {"type": "string", "default": ""},
                "agent_filter":    {"type": "string", "default": ""},
                "search_mode":     {"type": "string", "default": "hybrid"},
                "user_id":         {"type": "string", "default": ""},
                "scope":           {"type": "string", "default": ""},
                "as_of":           {"type": "string", "default": ""},
                "conversation_id": {"type": "string", "default": ""},
                "recency_bias":    {"type": "number", "default": 0.0},
                "adaptive_k":      {"type": "boolean", "default": False},
                "variant":         {"type": "string", "default": "", "description": "Variant filter applied uniformly to each DB. Pass a comma-separated list to use multi-variant IN filtering (passed through to memory_search_scored_impl)."},
                "fan_out_limit":   {"type": "integer", "default": 0, "description": "Cap on concurrent per-DB searches. 0 = unbounded (typical, since fan-out N is small)."},
            },
            "required": ["query", "databases"],
        },
        impl=memory_core.memory_search_multi_db_impl,
        is_async=True,
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_get",
        description="Retrieves a full MemoryItem; accepts full UUID or 8-char prefix; ambiguous prefixes return an error.",
        parameters={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Memory item id — 36-char UUID or 8-char prefix. "
                                   "Ambiguous prefixes return an error listing the matching ids.",
                },
            },
            "required": ["id"],
        },
        impl=memory_core.memory_get_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_update",
        description="Updates a MemoryItem by ID.",
        parameters={
            "type": "object",
            "properties": {
                "id":        {"type": "string", "description": "Memory item UUID."},
                "content":   {"type": "string", "description": "New content (empty = no change).", "default": ""},
                "title":     {"type": "string", "description": "New title (empty = no change).", "default": ""},
                "metadata":  {"type": "string", "description": "JSON-encoded metadata (empty = no change).", "default": ""},
                "importance": {"type": "number", "description": "New importance score (-1.0 = no change).", "default": -1.0},
                "reembed":   {"type": "boolean", "description": "Re-embed for semantic search.", "default": False},
                "refresh_on": {"type": "string", "description": "New refresh timestamp. 'clear' removes the reminder; empty = no change.", "default": ""},
                "refresh_reason": {"type": "string", "description": "New refresh reason. 'clear' removes; empty = no change.", "default": ""},
                "conversation_id": {"type": "string", "description": "New conversation id. 'clear' removes; empty = no change.", "default": ""},
            },
            "required": ["id"],
        },
        impl=memory_core.memory_update_impl,
        is_async=True,
        validators=(_memory_update_validator,),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_update_bulk",
        description=(
            "Apply many metadata-only updates in one transaction per chunk. "
            "Designed for curation passes that retroactively set retention, "
            "importance, or supersession metadata. Per-id reembed is NOT "
            "supported here (use memory_update for reembed, or re_embed_all "
            "for the bulk reembed case). Returns structured "
            "{succeeded, not_found, no_change, total}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "updates": {
                    "type": "array",
                    "description": (
                        "List of update specs. Each MUST include `id`. "
                        "Any subset of {content, title, importance, metadata, "
                        "refresh_on, refresh_reason, conversation_id} may be "
                        "set. Field semantics match memory_update."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id":              {"type": "string"},
                            "content":         {"type": "string"},
                            "title":           {"type": "string"},
                            "importance":      {"type": "number"},
                            "metadata":        {"type": "string", "description": "JSON-encoded."},
                            "refresh_on":      {"type": "string"},
                            "refresh_reason":  {"type": "string"},
                            "conversation_id": {"type": "string"},
                        },
                        "required": ["id"],
                    },
                },
            },
            "required": ["updates"],
        },
        impl=memory_core.memory_update_bulk_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="curate_memory_apply",
        description=(
            "Deterministically apply a memory.db curator plan in ONE call. "
            "No LLM in the loop — the apply phase is a pure function over the "
            "structured plan. Replaces the agent-driven APPLY-mode loop. "
            "Plan sections: delete (soft, list of UUIDs), delete_hard (cascade, "
            "list of UUIDs), link (list of {from_id, to_id, relationship_type}), "
            "update (list of {id, importance, metadata, ...}). Any section may "
            "be omitted. Returns structured per-section results + summary."
        ),
        parameters={
            "type": "object",
            "properties": {
                "plan": {
                    "type": "object",
                    "description": "Curator plan; see tool description for schema.",
                    "properties": {
                        "delete":      {"type": "array", "items": {"type": "string"}},
                        "delete_hard": {"type": "array", "items": {"type": "string"}},
                        "link":        {"type": "array", "items": {"type": "object"}},
                        "update":      {"type": "array", "items": {"type": "object"}},
                    },
                },
            },
            "required": ["plan"],
        },
        impl=lambda plan: __import__("curator_apply").apply_memory_plan(plan),
        is_async=False,
        validators=(),
        default_allowed=False,  # destructive: bulk deletes + writes
        inject_agent_id=False,
    ),
    ToolSpec(
        name="curate_chatlog_apply",
        description=(
            "Deterministically apply a chatlog.db curator plan in ONE call. "
            "No LLM in the loop. Plan sections: decay (True/dict to run "
            "chatlog_decay), dedup (list of {keep_id, drop_ids}), promote "
            "(list of {ids, target_type}), prune (list of {conversation_id, "
            "reason}). Any section may be omitted. Returns structured per-"
            "section results + summary."
        ),
        parameters={
            "type": "object",
            "properties": {
                "plan": {
                    "type": "object",
                    "description": "Chatlog curator plan; see tool description for schema.",
                    "properties": {
                        "decay":   {"type": "boolean"},
                        "dedup":   {"type": "array", "items": {"type": "object"}},
                        "promote": {"type": "array", "items": {"type": "object"}},
                        "prune":   {"type": "array", "items": {"type": "object"}},
                    },
                },
                "db_path": {
                    "type": "string",
                    "description": "Optional chatlog DB path override.",
                    "default": "",
                },
            },
            "required": ["plan"],
        },
        impl=lambda plan, db_path="": __import__("curator_apply").apply_chatlog_plan(
            plan, db_path=db_path or None
        ),
        is_async=False,
        validators=(),
        default_allowed=False,  # destructive
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_delete",
        description=(
            "Deletes a MemoryItem (soft or hard). id MUST be the full UUID — a "
            "prefix is rejected (full UUID required for mutation safety; "
            "memory_get accepts a prefix, this does not)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "id":   {"type": "string", "description": "FULL UUID of the memory (not an 8-char prefix — a prefix is rejected for mutation safety)."},
                "hard": {"type": "boolean", "description": "Hard delete (permanent).", "default": False},
            },
            "required": ["id"],
        },
        impl=memory_core.memory_delete_impl,
        is_async=False,
        validators=(_memory_delete_validator,),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_delete_bulk",
        description=(
            "Deletes a list of MemoryItems (soft or hard) in one transaction per chunk. "
            "Use for curation/dedup passes deleting many items; falls back to per-id "
            "memory_delete behavior with the same hard-cascade semantics. Returns a "
            "structured {succeeded, not_found, mode} dict."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ids":  {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of memory item UUIDs to delete.",
                },
                "hard": {"type": "boolean", "description": "Hard delete (permanent).", "default": False},
            },
            "required": ["ids"],
        },
        impl=memory_core.memory_delete_bulk_impl,
        is_async=False,
        validators=(),
        default_allowed=False,  # destructive — gated by MCP_PROXY_ALLOW_DESTRUCTIVE
        inject_agent_id=False,
    ),
    ToolSpec(
        name="conversation_start",
        description="Starts a new conversation thread.",
        parameters={
            "type": "object",
            "properties": {
                "title":    {"type": "string", "description": "Conversation title."},
                "agent_id": {"type": "string", "description": "Owning agent id.", "default": ""},
                "model_id": {"type": "string", "description": "Originating model id.", "default": ""},
                "tags":     {"type": "string", "description": "Comma-separated tags.", "default": ""},
            },
            "required": ["title"],
        },
        impl=memory_core.conversation_start_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="conversation_append",
        description="Appends a message to a conversation.",
        parameters={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Conversation UUID."},
                "role":            {"type": "string", "description": "Message role (e.g., 'user', 'assistant')."},
                "content":         {"type": "string", "description": "Message body."},
                "agent_id":        {"type": "string", "description": "Agent adding the message.", "default": ""},
                "model_id":        {"type": "string", "description": "Model that generated the message.", "default": ""},
                "embed":           {"type": "boolean", "description": "Embed for semantic search.", "default": True},
            },
            "required": ["conversation_id", "role", "content"],
        },
        impl=memory_core.conversation_append_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="conversation_search",
        description="Search messages across conversations using hybrid semantic/keyword search.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "k":     {"type": "integer", "description": "Max results (1-100).", "default": 8},
            },
            "required": ["query"],
        },
        impl=_conversation_search_impl,
        is_async=True,
        validators=(_memory_search_validator,),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="conversation_summarize",
        description="Summarize a conversation into key points using the local LLM.",
        parameters={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Conversation UUID."},
                "threshold":       {"type": "integer", "description": "Min message count to summarize.", "default": 20},
            },
            "required": ["conversation_id"],
        },
        impl=memory_core.conversation_summarize_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chroma_sync",
        description="Bi-directional sync between local SQLite and ChromaDB.",
        parameters={
            "type": "object",
            "properties": {
                "max_items":     {"type": "integer", "description": "Max items per batch.", "default": 50},
                "direction":     {"type": "string", "enum": ["both", "to_chroma", "from_chroma"], "description": "Sync direction.", "default": "both"},
                "reset_stalled": {"type": "boolean", "description": "Reset stalled sync records.", "default": True},
            },
            "required": [],
        },
        impl=memory_sync.chroma_sync_impl,
        is_async=True,
        validators=(),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_maintenance",
        description="Runs maintenance tasks on the memory store.",
        parameters={
            "type": "object",
            "properties": {
                "decay":                   {"type": "boolean", "description": "Apply importance decay.", "default": True},
                "purge_expired":           {"type": "boolean", "description": "Delete expired items.", "default": True},
                "prune_orphan_embeddings": {"type": "boolean", "description": "Remove orphaned embeddings.", "default": True},
            },
            "required": [],
        },
        impl=memory_maintenance.memory_maintenance_impl,
        is_async=False,
        validators=(),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_consolidate",
        description="Consolidate old memories of the same type into summaries using the local LLM. Reduces clutter while preserving knowledge.",
        parameters={
            "type": "object",
            "properties": {
                "type_filter":  {"type": "string", "description": "Restrict to a memory type.", "default": ""},
                "agent_filter": {"type": "string", "description": "Restrict to an agent id.", "default": ""},
                "threshold":    {"type": "integer", "description": "Min items to consolidate.", "default": 20},
            },
            "required": [],
        },
        impl=memory_maintenance.memory_consolidate_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_dedup",
        description=(
            "Find (and optionally soft-delete) near-duplicate memory items by "
            "cosine similarity over embeddings. Returns "
            "{count, groups: [{a, b, title_a, title_b, score}, ...], threshold, "
            "scanned, applied}. Use dry_run=True (default) for a preview; "
            "dry_run=False soft-deletes the second item of each pair."
        ),
        parameters={
            "type": "object",
            "properties": {
                "threshold": {"type": "number", "description": "Cosine similarity threshold in [0, 1]. Higher = stricter near-duplicate.", "default": 0.92},
                "dry_run":   {"type": "boolean", "description": "Preview without applying.", "default": True},
                "limit":     {"type": "integer", "description": "Cap on returned groups (0 = no cap). count reflects true total regardless.", "default": 0},
            },
            "required": [],
        },
        impl=memory_maintenance.memory_dedup_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_feedback",
        description="Provide feedback on a memory item to improve quality.",
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Memory item UUID."},
                "feedback":  {"type": "string", "enum": ["useful", "not_useful", "misleading"], "description": "Feedback type.", "default": "useful"},
            },
            "required": ["memory_id"],
        },
        impl=memory_maintenance.memory_feedback_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_history",
        description="Returns the change history (audit trail) for a memory item. Tracks create, update, delete, and supersede events.",
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Memory item UUID."},
                "limit":     {"type": "integer", "description": "Max history records.", "default": 20},
            },
            "required": ["memory_id"],
        },
        impl=memory_core.memory_history_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_link",
        description="Creates a directional link between two memory items. Valid types: related, supports, contradicts, extends, supersedes, references, consolidates, message, handoff.",
        parameters={
            "type": "object",
            "properties": {
                "from_id":            {"type": "string", "description": "Source memory UUID."},
                "to_id":              {"type": "string", "description": "Target memory UUID."},
                "relationship_type":  {"type": "string", "enum": ["related", "supports", "contradicts", "extends", "supersedes", "references", "consolidates", "message", "handoff"], "description": "Link type.", "default": "related"},
            },
            "required": ["from_id", "to_id"],
        },
        impl=memory_core.memory_link_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_link_bulk",
        description=(
            "Create many memory_relationships rows in one transaction per chunk. "
            "Use for curation passes adding many LINK edges (device pairs, "
            "supersession chains, etc.). Validates existence of every referenced "
            "memory_id; skips duplicates without raising. Returns a structured "
            "{created, skipped_missing, skipped_duplicate, total} dict."
        ),
        parameters={
            "type": "object",
            "properties": {
                "links": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "from_id":           {"type": "string"},
                            "to_id":             {"type": "string"},
                            "relationship_type": {"type": "string", "enum": ["related", "supports", "contradicts", "extends", "supersedes", "references", "consolidates", "message", "handoff"]},
                        },
                        "required": ["from_id", "to_id"],
                    },
                    "description": "List of link specs. relationship_type per entry overrides the outer default.",
                },
                "relationship_type": {
                    "type": "string",
                    "enum": ["related", "supports", "contradicts", "extends", "supersedes", "references", "consolidates", "message", "handoff"],
                    "description": "Default link type for entries that omit it.",
                    "default": "related",
                },
            },
            "required": ["links"],
        },
        impl=memory_core.memory_link_bulk_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_graph",
        description="Returns the local graph neighborhood of a memory item (connected memories up to N hops, max 3).",
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Memory item UUID."},
                "depth":     {"type": "integer", "description": "Traversal depth (1-3).", "default": 1},
            },
            "required": ["memory_id"],
        },
        impl=memory_core.memory_graph_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_verify",
        description="Verify content integrity by comparing stored hash with computed hash. Returns OK if content hasn't been tampered with.",
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Memory item UUID."},
            },
            "required": ["id"],
        },
        impl=_memory_verify_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_set_retention",
        description="Set or update per-agent memory retention policy. Controls max memory count, TTL expiry, and auto-archival.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id":     {"type": "string", "description": "Agent id for policy."},
                "max_memories": {"type": "integer", "description": "Max items to retain.", "default": 1000},
                "ttl_days":     {"type": "integer", "description": "Time-to-live in days (0 = no limit).", "default": 0},
                "auto_archive": {"type": "integer", "description": "Auto-archive threshold.", "default": 1},
            },
            "required": ["agent_id"],
        },
        impl=memory_maintenance.memory_set_retention_impl,
        is_async=False,
        validators=(_memory_set_retention_validator,),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_export",
        description="Export memories as portable JSON. Filter by agent, type, or date.",
        parameters={
            "type": "object",
            "properties": {
                "agent_filter": {"type": "string", "description": "Restrict to an agent id.", "default": ""},
                "type_filter":  {"type": "string", "description": "Restrict to a memory type.", "default": ""},
                "since":        {"type": "string", "description": "ISO-8601 start date.", "default": ""},
            },
            "required": [],
        },
        impl=memory_maintenance.memory_export_impl,
        is_async=False,
        validators=(),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_import",
        description="Import memories from a JSON export. UPSERT semantics — safe to re-run.",
        parameters={
            "type": "object",
            "properties": {
                "data": {"type": "string", "description": "JSON export string."},
            },
            "required": ["data"],
        },
        impl=memory_maintenance.memory_import_impl,
        is_async=False,
        validators=(),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="gdpr_export",
        description="Export all memories for a data subject (GDPR data portability). Returns JSON with all memory items for the given user_id.",
        parameters={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "Data subject id."},
            },
            "required": ["user_id"],
        },
        impl=memory_maintenance.gdpr_export_impl,
        is_async=False,
        validators=(_gdpr_user_id_validator,),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="gdpr_forget",
        description="Right to be forgotten — hard-deletes ALL data for a user_id including memories, embeddings, relationships, and history.",
        parameters={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "Data subject id to forget."},
            },
            "required": ["user_id"],
        },
        impl=memory_maintenance.gdpr_forget_impl,
        is_async=False,
        validators=(_gdpr_user_id_validator,),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="enrich_pending",
        description="Enrich pending memory items with SLM-distilled facts. Default dry_run=true reports count + ETA; pass dry_run=false to execute.",
        parameters={
            "type": "object",
            "properties": {
                "dry_run":           {"type": "boolean", "default": True, "description": "If true, report count + ETA without executing; if false, execute enrichment."},
                "limit":             {"type": "integer", "default": 0, "description": "Max items to enrich (0 = no limit)."},
                "allowed_variants":  {"type": "array", "default": [], "description": "Variant names to include in enrichment (if empty, use default)."},
            },
            "required": [],
        },
        impl=memory_core.enrich_pending_impl,
        is_async=True,
        validators=(),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_cost_report",
        description="Returns current session operation counts and estimated token usage for memory operations.",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        impl=memory_core.memory_cost_report_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_handoff",
        description=(
            "Hand off a task from one agent to another. Writes a new handoff-type memory "
            "owned by to_agent and links it to the given context memories with 'handoff' edges. "
            "Returns a confirmation string with the new memory id."
        ),
        parameters={
            "type": "object",
            "properties": {
                "from_agent":  {"type": "string", "description": "Sending agent id."},
                "to_agent":    {"type": "string", "description": "Receiving agent id."},
                "task":        {"type": "string", "description": "What the receiver should do."},
                "context_ids": {"type": "array", "items": {"type": "string"}, "description": "Memory ids to link via 'handoff' edges.", "default": []},
                "note":        {"type": "string", "description": "Optional free-text note.", "default": ""},
                "task_id":     {"type": "string", "description": "Optional tracked task id.", "default": ""},
            },
            "required": ["from_agent", "to_agent", "task"],
        },
        impl=memory_core.memory_handoff_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_inbox",
        description="List handoff messages addressed to agent_id, newest first. Pass unread_only=False to include already-acked items.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id":     {"type": "string", "description": "Receiving agent id."},
                "unread_only":  {"type": "boolean", "description": "Show only unread messages.", "default": True},
                "limit":        {"type": "integer", "description": "Max messages to return.", "default": 20},
            },
            "required": ["agent_id"],
        },
        impl=memory_core.memory_inbox_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=True,
    ),
    ToolSpec(
        name="memory_inbox_ack",
        description="Mark a handoff memory as read (sets read_at = now).",
        parameters={
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Handoff memory UUID."},
            },
            "required": ["memory_id"],
        },
        impl=memory_core.memory_inbox_ack_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_refresh_queue",
        description=(
            "List memories whose refresh_on timestamp has arrived and need review. "
            "Read-only — to actually refresh a memory, call memory_update with new "
            "content/refresh_on. Pass include_future=True to see all memories with "
            "refresh_on set, not just overdue ones."
        ),
        parameters={
            "type": "object",
            "properties": {
                "agent_id":       {"type": "string", "description": "Restrict to memories owned by this agent.", "default": ""},
                "limit":          {"type": "integer", "description": "Max rows to return (1-500).", "default": 50},
                "include_future": {"type": "boolean", "description": "Include memories whose refresh_on is still in the future.", "default": False},
            },
            "required": [],
        },
        impl=memory_core.memory_refresh_queue_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=True,
    ),
    ToolSpec(
        name="agent_register",
        description="Register an agent (UPSERT). Sets status=active, last_seen=now.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id":    {"type": "string", "description": "Unique agent identifier."},
                "role":        {"type": "string", "description": "Agent role or function.", "default": ""},
                "capabilities": {"type": "array", "items": {"type": "string"}, "description": "List of capabilities.", "default": []},
                "metadata":    {"type": "object", "description": "Free-form metadata.", "default": {}},
            },
            "required": ["agent_id"],
        },
        impl=memory_core.agent_register_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="agent_heartbeat",
        description="Update last_seen and set status=active. Errors if not registered.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Unique agent identifier."},
            },
            "required": ["agent_id"],
        },
        impl=memory_core.agent_heartbeat_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=True,
    ),
    ToolSpec(
        name="agent_list",
        description="List registered agents, optionally filtered by status and/or role.",
        parameters={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by agent status.", "default": ""},
                "role":   {"type": "string", "description": "Filter by agent role.", "default": ""},
            },
            "required": [],
        },
        impl=memory_core.agent_list_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="agent_get",
        description="Get full record for one registered agent.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Unique agent identifier."},
            },
            "required": ["agent_id"],
        },
        impl=memory_core.agent_get_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="agent_offline",
        description="Mark an agent as offline.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Unique agent identifier."},
            },
            "required": ["agent_id"],
        },
        impl=memory_core.agent_offline_impl,
        is_async=False,
        validators=(),
        default_allowed=False,
        inject_agent_id=True,
    ),
    ToolSpec(
        name="notify",
        description="Send a notification to an agent. Lightweight wake signal — agents poll notifications_poll.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Recipient agent id."},
                "kind":     {"type": "string", "description": "Notification kind/type."},
                "payload":  {"type": "object", "description": "Free-form notification data.", "default": {}},
            },
            "required": ["agent_id", "kind"],
        },
        impl=memory_core.notify_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="notifications_poll",
        description="List notifications addressed to agent_id, newest first.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id":    {"type": "string", "description": "Recipient agent id."},
                "unread_only": {"type": "boolean", "description": "Show only unread notifications.", "default": True},
                "limit":       {"type": "integer", "description": "Max notifications to return.", "default": 20},
            },
            "required": ["agent_id"],
        },
        impl=memory_core.notifications_poll_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=True,
    ),
    ToolSpec(
        name="notifications_ack",
        description="Mark one notification as read.",
        parameters={
            "type": "object",
            "properties": {
                "notification_id": {"type": "integer", "description": "Notification ID."},
            },
            "required": ["notification_id"],
        },
        impl=memory_core.notifications_ack_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="notifications_ack_all",
        description="Bulk-ack all unread notifications for an agent. Returns count acked.",
        parameters={
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "Agent id."},
            },
            "required": ["agent_id"],
        },
        impl=memory_core.notifications_ack_all_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=True,
    ),
    ToolSpec(
        name="task_create",
        description="Create a new task in 'pending' state. Returns task id.",
        parameters={
            "type": "object",
            "properties": {
                "title":          {"type": "string", "description": "Task title."},
                "created_by":     {"type": "string", "description": "Agent or user that created the task."},
                "description":    {"type": "string", "description": "Longer description.", "default": ""},
                "owner_agent":    {"type": "string", "description": "Initial owner (blank = unassigned).", "default": ""},
                "parent_task_id": {"type": "string", "description": "Optional parent task id for sub-tasks.", "default": ""},
                "metadata":       {"type": "object", "description": "Free-form metadata.", "default": {}},
            },
            "required": ["title", "created_by"],
        },
        impl=memory_core.task_create_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_assign",
        description="Assign a task to an owner. Sets state=in_progress and notifies the new owner.",
        parameters={
            "type": "object",
            "properties": {
                "task_id":     {"type": "string", "description": "Task UUID."},
                "owner_agent": {"type": "string", "description": "Agent to assign to."},
            },
            "required": ["task_id", "owner_agent"],
        },
        impl=memory_core.task_assign_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_update",
        description="Partial update for a task. Validates state transitions. On terminal state, sets completed_at.",
        parameters={
            "type": "object",
            "properties": {
                "task_id":     {"type": "string", "description": "Task UUID."},
                "state":       {"type": "string", "enum": ["", "pending", "in_progress", "blocked", "completed", "failed", "cancelled"], "description": "New state (empty = no change).", "default": ""},
                "description": {"type": "string", "description": "New description (empty = no change).", "default": ""},
                "metadata":    {"type": "object", "description": "New metadata (empty = no change).", "default": {}},
                "actor":       {"type": "string", "description": "Actor making the update.", "default": ""},
            },
            "required": ["task_id"],
        },
        impl=memory_core.task_update_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_delete",
        description="Delete a task. Soft-delete (default) sets a tombstone that propagates via pg_sync to the warehouse and peers. Hard-delete removes the row locally and requires a prior soft-delete.",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task UUID."},
                "hard":    {"type": "boolean", "description": "If true, permanently remove an already-tombstoned row from local SQLite.", "default": False},
                "actor":   {"type": "string", "description": "Actor performing the delete (audit log).", "default": ""},
            },
            "required": ["task_id"],
        },
        impl=memory_core.task_delete_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_set_result",
        description="Set the result memory pointer for a task. Does NOT change state.",
        parameters={
            "type": "object",
            "properties": {
                "task_id":         {"type": "string", "description": "Task UUID."},
                "result_memory_id": {"type": "string", "description": "Result memory UUID."},
            },
            "required": ["task_id", "result_memory_id"],
        },
        impl=memory_core.task_set_result_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_get",
        description="Get full record for one task.",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task UUID."},
            },
            "required": ["task_id"],
        },
        impl=memory_core.task_get_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_list",
        description="List tasks with optional filters. Newest updated first.",
        parameters={
            "type": "object",
            "properties": {
                "owner_agent":   {"type": "string", "description": "Filter by owner agent.", "default": ""},
                "state":         {"type": "string", "description": "Filter by task state.", "default": ""},
                "parent_task_id": {"type": "string", "description": "Filter by parent task id.", "default": ""},
                "limit":         {"type": "integer", "description": "Max tasks to return.", "default": 50},
            },
            "required": [],
        },
        impl=memory_core.task_list_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_tree",
        description="Render a recursive subtree of tasks rooted at root_task_id.",
        parameters={
            "type": "object",
            "properties": {
                "root_task_id": {"type": "string", "description": "Root task UUID."},
                "max_depth":    {"type": "integer", "description": "Max recursion depth.", "default": 3},
            },
            "required": ["root_task_id"],
        },
        impl=memory_core.task_tree_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    # ── Chat log subsystem ────────────────────────────────────────────────────
    ToolSpec(
        name="chatlog_write",
        description=(
            "Append one chat turn to the chat log DB. Provenance "
            "(host_agent, provider, model_id, conversation_id) is required. "
            "Writes are async-queued — returns the row id immediately."
        ),
        parameters={
            "type": "object",
            "properties": {
                "content":         {"type": "string",  "description": "Message text."},
                "role":            {"type": "string",  "description": "user|assistant|system|tool"},
                "conversation_id": {"type": "string",  "description": "Session/conversation UUID."},
                "host_agent":      {"type": "string",  "description": "Client: claude-code|gemini-cli|opencode|aider"},
                "provider":        {"type": "string",  "description": "Model provider: anthropic|google|openai|local|xai|deepseek|mistral|meta|other"},
                "model_id":        {"type": "string",  "description": "Exact model id, e.g. claude-opus-4-7"},
                "turn_index":      {"type": "integer", "description": "0-based turn index within conversation."},
                "agent_id":        {"type": "string",  "description": "Client agent id (host:user@machine).", "default": ""},
                "user_id":         {"type": "string",  "description": "Owning user id.", "default": ""},
                "metadata":        {"type": "string",  "description": "Extra metadata JSON string.", "default": "{}"},
                "tokens_in":       {"type": "integer", "description": "Prompt tokens (null if unknown)."},
                "tokens_out":      {"type": "integer", "description": "Completion tokens (null if unknown)."},
                "cost_usd":        {"type": "number",  "description": "Cost in USD (null → computed from price table)."},
                "latency_ms":      {"type": "integer", "description": "End-to-end request latency."},
            },
            "required": ["content", "role", "conversation_id", "host_agent", "provider", "model_id"],
        },
        impl=chatlog_core.chatlog_write_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chatlog_write_bulk",
        description="Bulk-append N chat turns. Each item needs the same required fields as chatlog_write.",
        parameters={
            "type": "object",
            "properties": {
                "items": {"type": "array",   "description": "List of chat-turn dicts."},
                "embed": {"type": "boolean", "description": "Reserved; ignored — sweeper handles.", "default": False},
            },
            "required": ["items"],
        },
        impl=chatlog_core.chatlog_write_bulk_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chatlog_search",
        description="Search chat_log rows. FTS5 keyword when query is non-empty; filter-only when empty.",
        parameters={
            "type": "object",
            "properties": {
                "query":           {"type": "string", "description": "FTS5 query; empty → filter-only listing."},
                "k":               {"type": "integer","description": "Max results.", "default": 8},
                "conversation_id": {"type": "string", "description": "Filter by conversation.", "default": ""},
                "host_agent":      {"type": "string", "description": "Filter by host agent.",   "default": ""},
                "provider":        {"type": "string", "description": "Filter by provider.",     "default": ""},
                "model_id":        {"type": "string", "description": "Filter by model id.",     "default": ""},
                "agent_id":        {"type": "string", "description": "Filter by agent id.",     "default": ""},
                "search_mode":     {"type": "string", "description": "hybrid|fts|vector (integrated mode only).", "default": "hybrid"},
                "since":           {"type": "string", "description": "ISO-8601 lower bound on created_at.", "default": ""},
                "until":           {"type": "string", "description": "ISO-8601 upper bound on created_at.", "default": ""},
            },
            "required": ["query"],
        },
        impl=chatlog_core.chatlog_search_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chatlog_promote",
        description=(
            "Promote chat_log rows into the main memory DB under a new type "
            "(default 'conversation'). ATTACH + INSERT SELECT in separate/hybrid; "
            "UPDATE type in integrated."
        ),
        parameters={
            "type": "object",
            "properties": {
                "ids":             {"type": "array",   "description": "Specific row ids to promote."},
                "conversation_id": {"type": "string",  "description": "Promote all rows in a conversation.", "default": ""},
                "since":           {"type": "string",  "description": "Promote rows at-or-after this ISO-8601.", "default": ""},
                "until":           {"type": "string",  "description": "Promote rows at-or-before this ISO-8601.", "default": ""},
                "copy":            {"type": "boolean", "description": "If false, delete source rows after copy.", "default": True},
                "target_type":     {"type": "string",  "description": "Type assigned in main DB.", "default": "conversation"},
            },
            "required": [],
        },
        impl=chatlog_core.chatlog_promote_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chatlog_list_conversations",
        description="List distinct conversation_ids with turn counts and timespans.",
        parameters={
            "type": "object",
            "properties": {
                "host_agent": {"type": "string",  "description": "Filter by host agent.", "default": ""},
                "limit":      {"type": "integer", "description": "Max conversations.",    "default": 50},
                "offset":     {"type": "integer", "description": "Pagination offset.",    "default": 0},
            },
            "required": [],
        },
        impl=chatlog_core.chatlog_list_conversations_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chatlog_cost_report",
        description="Aggregate tokens and cost_usd across chat_log rows. Groups: provider|model_id|host_agent|conversation_id|day.",
        parameters={
            "type": "object",
            "properties": {
                "since":    {"type": "string", "description": "ISO-8601 lower bound.",   "default": ""},
                "until":    {"type": "string", "description": "ISO-8601 upper bound.",   "default": ""},
                "group_by": {"type": "string", "description": "provider|model_id|host_agent|conversation_id|day", "default": "model_id"},
            },
            "required": [],
        },
        impl=chatlog_core.chatlog_cost_report_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chatlog_set_redaction",
        description="Flip redaction on/off and update patterns. Persists to memory/.chatlog_config.json.",
        parameters={
            "type": "object",
            "properties": {
                "enabled":             {"type": "boolean", "description": "Turn redaction on/off."},
                "patterns":            {"type": "array",   "description": "Enabled pattern groups."},
                "redact_pii":          {"type": "boolean", "description": "Also redact PII (email/phone/SSN)."},
                "custom_regex":        {"type": "array",   "description": "User-supplied regex patterns."},
                "store_original_hash": {"type": "boolean", "description": "Store SHA-256 of pre-scrub content in metadata."},
            },
            "required": ["enabled"],
        },
        impl=chatlog_core.chatlog_set_redaction_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chatlog_status",
        description=(
            "One-call health summary of the chat log subsystem: mode, DB paths, row "
            "counts, queue depth, spill files, embed backlog, hook timestamps, redaction state, warnings."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        impl=chatlog_status.chatlog_status_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="chatlog_rescrub",
        description="Re-apply redaction to existing chat_log rows. Requires redaction.enabled=true.",
        parameters={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string",  "description": "Filter by conversation.", "default": ""},
                "since":           {"type": "string",  "description": "ISO-8601 lower bound.",  "default": ""},
                "until":           {"type": "string",  "description": "ISO-8601 upper bound.",  "default": ""},
                "limit":           {"type": "integer", "description": "Max rows to process.",   "default": 10000},
            },
            "required": [],
        },
        impl=chatlog_core.chatlog_rescrub_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="entity_search",
        description="Search entities by canonical_name and optionally by entity_type. Returns list of matching entities with optional neighbor counts.",
        parameters={
            "type": "object",
            "properties": {
                "query":         {"type": "string",  "default": "", "description": "Search term matched against canonical_name (LIKE %query%)."},
                "entity_type":   {"type": "string",  "default": "", "description": "Filter by entity type (if provided)."},
                "limit":         {"type": "integer", "default": 10,  "description": "Max results to return."},
                "with_neighbors": {"type": "boolean", "default": False, "description": "If true, compute neighbor_count for each entity."},
            },
            "required": [],
        },
        impl=memory_core.entity_search_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="entity_get",
        description="Load a single entity with its full neighborhood: predecessors, successors, and linked memory items.",
        parameters={
            "type": "object",
            "properties": {
                "entity_id": {"type": "string", "description": "The entity ID to fetch."},
                "depth":     {"type": "integer", "default": 1, "description": "Graph depth for neighborhood walk (currently unused; reserved for future multi-hop)."},
            },
            "required": ["entity_id"],
        },
        impl=memory_core.entity_get_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_count_entities",
        description=(
            "Count distinct entities mentioned in a single conversation. "
            "Direct-index aggregation — no LLM, no embedding. Use this for "
            "'how many distinct X did I mention' inventory questions where "
            "top-k embedding retrieval would miss instances spread thinly "
            "across many turns. Returns {count, conversation_id, entity_type, "
            "pattern}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Required. The conversation to scope the count to. Empty / missing → ValueError (cross-conversation scans are not supported)."},
                "entity_type":     {"type": "string", "default": "", "description": "Optional type filter (e.g. 'product', 'place', 'person'). Empty = all types."},
                "pattern":         {"type": "string", "default": "", "description": "Optional case-insensitive substring filter on canonical_name. Max length 256."},
            },
            "required": ["conversation_id"],
        },
        impl=memory_core.count_entities_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_count_mentions",
        description=(
            "Per-entity mention frequency within a single conversation, "
            "sorted DESC by count. Use for 'what are the most-mentioned X' or "
            "'rank entities by frequency.' Returns {total, rows: [{entity_id, "
            "canonical_name, entity_type, mention_count}, ...]}."
        ),
        parameters={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Required. The conversation to scope the count to."},
                "entity_type":     {"type": "string", "default": "", "description": "Optional type filter."},
                "pattern":         {"type": "string", "default": "", "description": "Optional case-insensitive substring filter on canonical_name. Max length 256."},
                "limit":           {"type": "integer", "default": 0, "description": "Max rows to return. 0 = default (1000). Hard cap = 10000."},
            },
            "required": ["conversation_id"],
        },
        impl=memory_core.count_mentions_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="entity_mentions",
        description=(
            "List memory_ids that mention a specific entity in a single "
            "conversation. Pass either entity_id (preferred — exact match) or "
            "canonical_name (case-insensitive, optionally disambiguated by "
            "entity_type). Returns {entity_id, canonical_name, entity_type, "
            "total, memory_ids: [...]}. Caller fetches text via existing read "
            "paths (which carry their own authz). Companion to entity_search "
            "and entity_get; lives in the 'entity' domain."
        ),
        parameters={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "Required."},
                "entity_id":       {"type": "string", "default": "", "description": "Preferred lookup — the entities.id. If supplied, canonical_name and entity_type are ignored."},
                "canonical_name":  {"type": "string", "default": "", "description": "Alternative lookup. Case-insensitive exact match. One of entity_id OR canonical_name is required."},
                "entity_type":     {"type": "string", "default": "", "description": "Optional disambiguator when canonical_name is ambiguous across types."},
                "limit":           {"type": "integer", "default": 0, "description": "Max memory_ids to return. 0 = default (1000). Hard cap = 10000."},
            },
            "required": ["conversation_id"],
        },
        impl=memory_core.list_mentions_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="extract_pending",
        description="Extract pending entities from the queue. Default dry_run=true reports count + ETA; pass dry_run=false to execute.",
        parameters={
            "type": "object",
            "properties": {
                "dry_run":           {"type": "boolean", "default": True, "description": "If true, report count + ETA without executing; if false, execute extraction."},
                "limit":             {"type": "integer", "default": 0, "description": "Max items to extract (0 = no limit)."},
                "allowed_variants":  {"type": "array", "default": [], "description": "Variant names to include in extraction (if empty, use default)."},
            },
            "required": [],
        },
        impl=memory_core.extract_pending_impl,
        is_async=True,
        validators=(),
        default_allowed=False,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="embedder_status",
        description="Check the status of the local sovereign embedder server (default port 8082, override via M3_EMBED_FALLBACK_URL).",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        impl=memory_core.embedder_status_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="memory_doctor",
        description=(
            "Self-service diagnostic for the m3-memory embedding cascade. "
            "Probes tier-1 (in-proc GGUF), tier-2 (m3-embed-server :8082), "
            "DB integrity, and end-to-end embed roundtrip — all concurrently "
            "with bounded 2s per-probe timeouts. Returns a structured dict "
            "with status ('healthy' | 'degraded' | 'broken'), per-tier "
            "details, issues, and actionable recommendations. Use this when "
            "memory_search hangs, embeddings look wrong, or you're standing "
            "up a new deployment."
        ),
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
        },
        impl=memory_core.memory_doctor_impl,
        is_async=True,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),

    # ───────────────────────────────────────────────────────────────────────────
    # files-memory tools (FILE_INGESTION_PLAN.md phases 1-4).
    # All sync; the package's LLM and embed calls block (the embed cascade
    # uses asyncio.run internally, no need to mark these async here).
    # ───────────────────────────────────────────────────────────────────────────
    ToolSpec(
        name="files_ingest",
        description=(
            "Walk a directory and ingest supported files into files.db. "
            "Idempotent: same content_sha256 -> no-op; changed content -> "
            "new file_node version supersedes prior. Use extract_mode to "
            "opt into fact extraction; use original_path (or a "
            "<path>.m3meta.json sidecar) to point search results at a "
            "source-of-truth file when the ingested file is a conversion."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path":          {"type": "string", "description": "Directory (or single file) to ingest."},
                "include":       {"type": "array",  "items": {"type": "string"}, "description": "Glob patterns; only matching files are ingested.", "default": None},
                "exclude":       {"type": "array",  "items": {"type": "string"}, "description": "Glob patterns; matching files are skipped.", "default": None},
                "max_depth":     {"type": "integer", "description": "Max recursion depth (0 = root only).", "default": None},
                "corpus":        {"type": "string", "description": "Corpus tag (default: resolved from M3_FILES_CORPUS or 'default').", "default": None},
                "dry_run":       {"type": "boolean", "description": "Walk + count without writing.", "default": False},
                "force_size":    {"type": "boolean", "description": "Bypass the per-file size cap.", "default": False},
                "record_noops":  {"type": "boolean", "description": "Write 'unchanged_skipped' rows for audit.", "default": False},
                "extract_mode":  {"type": "string", "enum": ["none", "inline", "queue"], "description": "Fact-extraction mode.", "default": None},
                "original_path": {"type": "string", "description": "Pointer to source artifact when single-file ingest is a conversion.", "default": None},
            },
            "required": ["path"],
        },
        impl=_files_tools.files_ingest_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_search",
        description=(
            "Hybrid FTS5 + vector search over file-ingestion leaves. "
            "Default: current versions only. Set include_history=True for "
            "time-travel queries. Use `corpora` for fan-out across multiple corpora."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query":           {"type": "string", "description": "Free-text query."},
                "limit":           {"type": "integer", "description": "Max hits returned.", "default": 10},
                "corpus":          {"type": "string", "description": "Single-corpus scope filter.", "default": None},
                "corpora":         {"type": "array",  "items": {"type": "string"}, "description": "Fan-out across these corpora; overrides corpus.", "default": None},
                "filetype":        {"type": "string", "description": "Filter to one filetype ('markdown', 'pdf', ...).", "default": None},
                "include_history": {"type": "boolean", "description": "Include superseded leaves and file_nodes.", "default": False},
            },
            "required": ["query"],
        },
        impl=_files_tools.files_search_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_index",
        description=(
            "Return file-level summaries for triage (wiki-index primitive). "
            "Cheap-first retrieval -- no leaf content. Use BEFORE files_search "
            "to decide which files are worth deep-reading."
        ),
        parameters={
            "type": "object",
            "properties": {
                "corpus":          {"type": "string", "default": None},
                "corpora":         {"type": "array",  "items": {"type": "string"}, "default": None},
                "filetype":        {"type": "string", "default": None},
                "directory":       {"type": "string", "default": None},
                "filename_glob":   {"type": "string", "default": None},
                "include_history": {"type": "boolean", "default": False},
                "limit":           {"type": "integer", "default": 500},
            },
            "required": [],
        },
        impl=_files_tools.files_index_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_get",
        description="Fetch one record by UUID. Tries file_nodes then leaves.",
        parameters={
            "type": "object",
            "properties": {"uuid": {"type": "string", "description": "UUID of the file_node or leaf."}},
            "required": ["uuid"],
        },
        impl=_files_tools.files_get_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_stats",
        description="Corpus-level counters: file_nodes, leaves, embed coverage, by-filetype.",
        parameters={
            "type": "object",
            "properties": {"corpus": {"type": "string", "default": None}},
            "required": [],
        },
        impl=_files_tools.files_stats_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_health",
        description="DB integrity + FTS5 sync check. Set rebuild=True to fix drift.",
        parameters={
            "type": "object",
            "properties": {"rebuild": {"type": "boolean", "default": False}},
            "required": [],
        },
        impl=_files_tools.files_health_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_extract_pending",
        description=(
            "Drain leaves with extraction_status='pending' through the LLM "
            "fact extractor. Used after a queue-mode ingest. Safe to call "
            "repeatedly."
        ),
        parameters={
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 100}},
            "required": [],
        },
        impl=_files_tools.files_extract_pending_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_promote",
        description=(
            "Promote (ascend) a fact / leaf / file_summary from files.db "
            "to memory.db. Source stays untouched; copy lands in memory.db "
            "with a metadata back-pointer. Idempotent."
        ),
        parameters={
            "type": "object",
            "properties": {
                "source_uuid": {"type": "string"},
                "reason":      {"type": "string", "default": ""},
                "mapped_type": {"type": "string", "description": "Override memory.db type (fact|knowledge|reference|...).", "default": None},
                "scope":       {"type": "string", "default": None},
                "importance":  {"type": "number", "default": 0.6},
            },
            "required": ["source_uuid"],
        },
        impl=_files_tools.files_promote_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_promotion_list",
        description=(
            "List existing promotions. source_superseded=True surfaces "
            "promotions whose source file has since been superseded -- "
            "candidates for review."
        ),
        parameters={
            "type": "object",
            "properties": {
                "source_file_node":   {"type": "string", "default": None},
                "source_superseded":  {"type": "boolean", "default": None},
                "limit":              {"type": "integer", "default": 100},
            },
            "required": [],
        },
        impl=_files_tools.files_promotion_list_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_promotable",
        description=(
            "List top promotion candidates by usage-weighted heuristic "
            "score. Suggestion-only; use files_promote to actually ascend any."
        ),
        parameters={
            "type": "object",
            "properties": {
                "limit":                       {"type": "integer", "default": 20},
                "min_score":                   {"type": "number",  "default": 0.30},
                "corpus":                      {"type": "string",  "default": None},
                "include_already_promoted":    {"type": "boolean", "default": False},
            },
            "required": [],
        },
        impl=_files_tools.files_promotable_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_dedup",
        description=(
            "Scan leaf embeddings for near-duplicates above cosine threshold. "
            "Detection only -- pairs land in semantic_dedup_candidates for "
            "human review."
        ),
        parameters={
            "type": "object",
            "properties": {
                "threshold":                 {"type": "number",  "default": 0.92},
                "max_pairs":                 {"type": "integer", "default": 500},
                "leaf_limit":                {"type": "integer", "default": 10000},
                "corpus":                    {"type": "string",  "default": None},
                "include_already_detected":  {"type": "boolean", "default": False},
            },
            "required": [],
        },
        impl=_files_tools.files_dedup_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_dedup_list",
        description="List near-duplicate candidate pairs with text snippets and paths.",
        parameters={
            "type": "object",
            "properties": {
                "reviewed":   {"type": "boolean", "default": False},
                "limit":      {"type": "integer", "default": 100},
                "min_cosine": {"type": "number",  "default": None},
            },
            "required": [],
        },
        impl=_files_tools.files_dedup_list_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_dedup_review",
        description=(
            "Record a review decision on a near-duplicate candidate: "
            "'kept' | 'merged' | 'ignored'."
        ),
        parameters={
            "type": "object",
            "properties": {
                "candidate_uuid": {"type": "string"},
                "action":         {"type": "string", "enum": ["kept", "merged", "ignored"]},
                "note":           {"type": "string", "default": ""},
            },
            "required": ["candidate_uuid", "action"],
        },
        impl=_files_tools.files_dedup_review_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_entity_coalesce",
        description=(
            "Detect provisional-entity coalescing candidates (quarantine noise + "
            "flag near-duplicate entities). Detection only -- never merges, never "
            "auto-applies; candidates land in entity_coalesce_candidates for "
            "review. dry_run=True estimates without writing or embedding."
        ),
        parameters={
            "type": "object",
            "properties": {
                "max_pairs": {"type": "integer", "default": 1000},
                "dry_run":   {"type": "boolean", "default": False},
                "corpus":    {"type": "string",  "default": None},
            },
            "required": [],
        },
        impl=_files_tools.files_entity_coalesce_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_entity_coalesce_list",
        description="List entity-coalescing candidate pairs (name + score + band).",
        parameters={
            "type": "object",
            "properties": {
                "reviewed":   {"type": "boolean", "default": False},
                "limit":      {"type": "integer", "default": 100},
                "min_cosine": {"type": "number",  "default": None},
            },
            "required": [],
        },
        impl=_files_tools.files_entity_coalesce_list_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_entity_coalesce_review",
        description=(
            "Record entity-coalescing review decisions in BULK: a list of "
            "{uuid, action} where action is 'merge' | 'related' | 'reject' | "
            "'defer'. Records intent only; materialize with "
            "files_entity_coalesce_apply."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reviews": {"type": "array"},
                "note":    {"type": "string", "default": ""},
            },
            "required": ["reviews"],
        },
        impl=_files_tools.files_entity_coalesce_review_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_entity_coalesce_apply",
        description=(
            "Apply the reversible same_as/cluster overlay. Union of explicit "
            "candidate_uuids (reviewed 'merge' or 'unapplied' tombstone) and -- "
            "if include_auto_merge -- the LATEST run's 'merge' band (or "
            "resolution_run). Members are never deleted; reverse with "
            "files_entity_coalesce_unapply. WRITES the core graph: a real apply "
            "MUST pass confirm=True; dry_run=True previews without writing."
        ),
        parameters={
            "type": "object",
            "properties": {
                "candidate_uuids":    {"type": "array",   "default": None},
                "include_auto_merge": {"type": "boolean", "default": False},
                "resolution_run":     {"type": "string",  "default": None},
                "dry_run":            {"type": "boolean", "default": False},
                "confirm":            {"type": "boolean", "default": False},
            },
            "required": [],
        },
        impl=_files_tools.files_entity_coalesce_apply_impl,
        is_async=False,
        validators=(),
        # Reversible by construction (writes a same_as/cluster overlay, never
        # deletes members; unapply fully restores). Not destructive per §6, so
        # not gated here — the impl's own confirm=True guard is the safety.
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_entity_coalesce_unapply",
        description=(
            "Reverse one coalescence cluster (drop edges, clear flags, strip "
            "aliases, tombstone the candidate so auto-merge won't resurrect it). "
            "Members are never deleted; re-apply via files_entity_coalesce_apply "
            "with candidate_uuids."
        ),
        parameters={
            "type": "object",
            "properties": {
                "cluster_id": {"type": "string"},
            },
            "required": ["cluster_id"],
        },
        impl=_files_tools.files_entity_coalesce_unapply_impl,
        is_async=False,
        validators=(),
        # Pure undo — removes only overlay edges/flags, restores state, deletes
        # no member data. Never gated.
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_staleness_review",
        description=(
            "Compare filesystem against files.db. Surfaces stale, "
            "touched-only, missing, new, failed-extraction, "
            "drifted-promotion files, and rename candidates. Report-only."
        ),
        parameters={
            "type": "object",
            "properties": {
                "directory": {"type": "string", "default": None},
                "corpus":    {"type": "string", "default": None},
                "rehash":    {"type": "boolean", "default": True},
                "limit":     {"type": "integer", "default": 200},
            },
            "required": [],
        },
        impl=_files_tools.files_staleness_review_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_link_rename",
        description=(
            "Re-point an existing file_node at a new path (rename / move). "
            "NOT a supersession -- content stays identical. Use this only "
            "when staleness review surfaces a rename candidate."
        ),
        parameters={
            "type": "object",
            "properties": {
                "missing_file_node_uuid": {"type": "string"},
                "new_path":               {"type": "string"},
                "expect_sha256":          {"type": "string", "default": None},
            },
            "required": ["missing_file_node_uuid", "new_path"],
        },
        impl=_files_tools.files_link_rename_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_corpus_create",
        description=(
            "Register a new corpus with optional default overrides. "
            "`default=True` marks this corpus as the installation's "
            "default (clears the flag on any prior default in the same "
            "transaction)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "corpus_id":    {"type": "string"},
                "description":  {"type": "string", "default": None},
                "extract_mode": {"type": "string", "enum": ["none", "inline", "queue"], "default": None},
                "scope":        {"type": "string", "default": None},
                "default":      {"type": "boolean", "default": False},
            },
            "required": ["corpus_id"],
        },
        impl=_files_tools.files_corpus_create_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_corpus_list",
        description="Enumerate corpora with row counts.",
        parameters={"type": "object", "properties": {}, "required": []},
        impl=_files_tools.files_corpus_list_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_corpus_get",
        description="Fetch a single corpus's settings + counts.",
        parameters={
            "type": "object",
            "properties": {"corpus_id": {"type": "string"}},
            "required": ["corpus_id"],
        },
        impl=_files_tools.files_corpus_get_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_corpus_set",
        description=(
            "Update settings for an existing corpus. None args are no-ops. "
            "Creates the corpus_settings row if absent."
        ),
        parameters={
            "type": "object",
            "properties": {
                "corpus_id":      {"type": "string"},
                "description":    {"type": "string", "default": None},
                "extract_mode":   {"type": "string", "enum": ["none", "inline", "queue"], "default": None},
                "scope":          {"type": "string", "default": None},
                "default":        {"type": "boolean", "default": None},
                "retention_days": {"type": "integer", "default": None},
            },
            "required": ["corpus_id"],
        },
        impl=_files_tools.files_corpus_set_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_corpus_delete",
        description=(
            "Delete a corpus's settings row. Cascade=True also deletes "
            "every file_node in the corpus -- DESTRUCTIVE. Without "
            "cascade, refuses when the corpus has file_nodes."
        ),
        parameters={
            "type": "object",
            "properties": {
                "corpus_id": {"type": "string"},
                "cascade":   {"type": "boolean", "default": False},
            },
            "required": ["corpus_id"],
        },
        impl=_files_tools.files_corpus_delete_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="files_watch_once",
        description=(
            "Single-pass staleness check + notification dispatch. Suitable "
            "for cron / scheduled runners. Notifications are emitted via "
            "the memory.db notifications inbox; cooldown suppresses "
            "duplicates within the window."
        ),
        parameters={
            "type": "object",
            "properties": {
                "directory":         {"type": "string",  "default": None},
                "corpus":            {"type": "string",  "default": None},
                "agent_id":          {"type": "string",  "default": "files_memory.watch"},
                "cooldown_seconds":  {"type": "number",  "default": 3600.0},
            },
            "required": [],
        },
        impl=_files_tools.files_watch_once_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
]


# ── Universal `database` parameter injection ─────────────────────────────────
# Every MCP tool gains an optional `database` argument so callers can route a
# single tool call to a non-default SQLite DB (separate stores for chatlog /
# memories / testing / benchmarking). Injection happens at module load so the
# catalog stays the single source of truth and schemas FastMCP introspects
# always include the field. The dispatcher (execute_tool and the
# memory_bridge wrapper) pops the value and activates it via active_database()
# before calling the impl — impl signatures do not change.
_DATABASE_PARAM_SCHEMA = {
    "type": "string",
    "description": (
        "Optional SQLite database path. Overrides M3_DATABASE env and the "
        "default memory/agent_memory.db for this call only. Empty = use default."
    ),
    "default": "",
}


def _inject_database_arg() -> None:
    for spec in TOOLS:
        props = spec.parameters.setdefault("properties", {})
        # Skip if some future spec already declared `database` explicitly.
        if "database" not in props:
            props["database"] = dict(_DATABASE_PARAM_SCHEMA)


_inject_database_arg()

_BY_NAME: dict[str, ToolSpec] = {t.name: t for t in TOOLS}
