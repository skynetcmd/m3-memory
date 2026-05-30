from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable

import memory_core

# ── Validation Constants ─────────────────────────────────────────────────────
MAX_CONTENT_SIZE = 50_000
MAX_QUERY_LENGTH = 2_000
MAX_K = 100
VALID_MEMORY_TYPES = frozenset({
    "note", "fact", "decision", "preference", "conversation", "message",
    "task", "code", "config", "observation", "plan", "summary", "snippet",
    "reference", "log", "home", "user_fact", "scratchpad", "auto",
    "knowledge", "event_extraction", "fact_enriched", "chat_log",
    "local_device", "network_config", "infrastructure", "home_automation",
    "migration-log", "security", "windows_only", "macos_only", "linux_only",
    "to_do",
})

# Entity-graph enums
VALID_ENTITY_TYPES = memory_core.VALID_ENTITY_TYPES
VALID_ENTITY_PREDICATES = memory_core.VALID_ENTITY_PREDICATES

# Canonical UUID shape (8-4-4-4-12 hex).
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
    if not _is_full_uuid(old_id):
        return ("Error: old_id must be the full UUID, not a prefix — "
                "full UUID required for mutation safety. "
                "Resolve the prefix first with memory_get (which accepts a "
                "prefix), then pass the returned full id.")
    args["old_id"] = old_id
    t = args.get("type", "")
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


def _pop_database(args: dict) -> str | None:
    db = args.pop("database", None)
    if isinstance(db, str):
        db = db.strip() or None
    return db
