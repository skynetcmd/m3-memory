"""m3-memory plugin — MemoryProvider interface.

Hybrid FTS5 + vector recall with MMR diversity rerank, a bitemporal model, KG
supersession, and async Observer/Reflector fact extraction — exposed to Hermes
Agent through the same MemoryProvider ABC the mem0 plugin uses.

Patterned on plugins/memory/mem0/__init__.py (threaded prefetch, circuit
breaker, three tools, register(ctx) entrypoint).

Config via environment variables:
  M3_USER_ID    — user identifier (default: hermes-user)
  M3_AGENT_ID   — agent identifier (default: hermes)
Or via $HERMES_HOME/m3.json.

Requires m3-memory's bin/ on PYTHONPATH so `import mcp_tool_catalog` resolves
(see m3client.py). No m3 server/proxy needed — calls run in-process.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)

# Circuit breaker — same policy as the mem0 provider.
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECS = 120


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Env vars with $HERMES_HOME/m3.json overrides (mem0 pattern)."""
    from hermes_constants import get_hermes_home

    config = {
        "user_id": os.environ.get("M3_USER_ID", "hermes-user"),
        "agent_id": os.environ.get("M3_AGENT_ID", "hermes"),
    }

    config_path = get_hermes_home() / "m3.json"
    if config_path.exists():
        try:
            file_cfg = json.loads(config_path.read_text(encoding="utf-8"))
            config.update({k: v for k, v in file_cfg.items()
                           if v is not None and v != ""})
        except Exception:
            pass

    return config


# ---------------------------------------------------------------------------
# Tool schemas — flat shape, matching the mem0 provider exactly.
# ---------------------------------------------------------------------------

PROFILE_SCHEMA = {
    "name": "m3_profile",
    "description": (
        "Retrieve stored stable facts about the user — preferences, decisions, "
        "project context. Use at conversation start."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

SEARCH_SCHEMA = {
    "name": "m3_search",
    "description": (
        "Hybrid semantic + keyword search over long-term memory (FTS5 + vector "
        "+ MMR diversity rerank). Returns facts ranked by relevance."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to search for."},
            "top_k": {"type": "integer", "description": "Max results (default: 10, max: 50)."},
        },
        "required": ["query"],
    },
}

CONCLUDE_SCHEMA = {
    "name": "m3_conclude",
    "description": (
        "Store a durable fact about the user, verbatim (no LLM re-extraction). "
        "Use for explicit preferences, corrections, or decisions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "conclusion": {"type": "string", "description": "The fact to store."},
        },
        "required": ["conclusion"],
    },
}


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class M3MemoryProvider(MemoryProvider):
    """m3-memory: async extraction (Observer/Reflector), hybrid recall, KG
    supersession, exposed through the MemoryProvider ABC."""

    def __init__(self):
        self._config = None
        self._client = None
        self._client_lock = threading.Lock()
        self._user_id = "hermes-user"
        self._agent_id = "hermes"
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread = None
        self._sync_thread = None
        # Circuit breaker state
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    @property
    def name(self) -> str:
        return "m3"

    def is_available(self) -> bool:
        # Available iff the m3 catalog module is on the path. Use find_spec, NOT
        # `import mcp_tool_catalog`: discovery runs is_available() for every
        # provider at startup, and actually importing the catalog pulls in
        # memory_core + the embedder load path (slow, and historically a WMI
        # stall on Py3.14/Windows). find_spec only resolves the module location
        # — no execution, no side effects, instant.
        import importlib.util
        try:
            return importlib.util.find_spec("mcp_tool_catalog") is not None
        except (ImportError, ValueError):
            return False

    def save_config(self, values, hermes_home):
        import json as _json
        from pathlib import Path
        config_path = Path(hermes_home) / "m3.json"
        existing = {}
        if config_path.exists():
            try:
                existing = _json.loads(config_path.read_text())
            except Exception:
                pass
        existing.update(values)
        try:
            from utils import atomic_json_write
            atomic_json_write(config_path, existing, mode=0o600)
        except Exception:
            config_path.write_text(_json.dumps(existing, indent=2))

    def get_config_schema(self):
        return [
            {"key": "user_id", "description": "User identifier", "default": "hermes-user"},
            {"key": "agent_id", "description": "Agent identifier", "default": "hermes"},
        ]

    def _get_client(self):
        """Thread-safe lazy M3Client."""
        with self._client_lock:
            if self._client is not None:
                return self._client
            try:
                from .m3client import M3Client
            except Exception as e:
                # Widened from ImportError: a slow/partial import of the m3
                # catalog under threaded tool execution can surface as other
                # exception types. Report the concrete error rather than a
                # generic "PYTHONPATH?" guess.
                raise RuntimeError(f"m3 client unavailable: {type(e).__name__}: {e}")
            self._client = M3Client(agent_id=self._agent_id)
            return self._client

    # ── circuit breaker (verbatim from mem0) ────────────────────────────
    def _is_breaker_open(self) -> bool:
        if self._consecutive_failures < _BREAKER_THRESHOLD:
            return False
        if time.monotonic() >= self._breaker_open_until:
            self._consecutive_failures = 0
            return False
        return True

    def _record_success(self):
        self._consecutive_failures = 0

    def _record_failure(self):
        self._consecutive_failures += 1
        if self._consecutive_failures >= _BREAKER_THRESHOLD:
            self._breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECS
            logger.warning(
                "m3 circuit breaker tripped after %d consecutive failures. "
                "Pausing calls for %ds.",
                self._consecutive_failures, _BREAKER_COOLDOWN_SECS,
            )

    def initialize(self, session_id: str, **kwargs) -> None:
        self._config = _load_config()
        # gateway-provided user_id wins for per-user scoping (mem0 behavior)
        self._user_id = kwargs.get("user_id") or self._config.get("user_id", "hermes-user")
        self._agent_id = self._config.get("agent_id", "hermes")

    def system_prompt_block(self) -> str:
        return (
            "# m3 Memory\n"
            f"Active. User: {self._user_id}.\n"
            "Use m3_search to find memories, m3_conclude to store a verbatim "
            "fact, m3_profile for a full overview."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._prefetch_thread and self._prefetch_thread.is_alive():
            self._prefetch_thread.join(timeout=3.0)
        with self._prefetch_lock:
            result = self._prefetch_result
            self._prefetch_result = ""
        if not result:
            return ""
        return f"## m3 Memory\n{result}"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if self._is_breaker_open():
            return

        def _run():
            try:
                client = self._get_client()
                results = client.search(query=query, user_id=self._user_id, top_k=5)
                if results:
                    lines = [r.get("content", "") for r in results if r.get("content")]
                    with self._prefetch_lock:
                        self._prefetch_result = "\n".join(f"- {ln}" for ln in lines)
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.debug("m3 prefetch failed: %s", e)

        self._prefetch_thread = threading.Thread(
            target=_run, daemon=True, name="m3-prefetch"
        )
        self._prefetch_thread.start()

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: List[Dict[str, Any]] | None = None,
    ) -> None:
        """Enqueue the turn into m3's chatlog; Observer/Reflector extract +
        supersede async (cheaper per-turn than mem0's synchronous add())."""
        if self._is_breaker_open():
            return

        def _sync():
            try:
                client = self._get_client()
                client.chatlog_write(
                    user_id=self._user_id,
                    session_id=session_id,
                    user_content=user_content,
                    assistant_content=assistant_content,
                )
                self._record_success()
            except Exception as e:
                self._record_failure()
                logger.warning("m3 sync failed: %s", e)

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)

        self._sync_thread = threading.Thread(target=_sync, daemon=True, name="m3-sync")
        self._sync_thread.start()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [PROFILE_SCHEMA, SEARCH_SCHEMA, CONCLUDE_SCHEMA]

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if self._is_breaker_open():
            return json.dumps({
                "error": "m3 temporarily unavailable (multiple consecutive "
                         "failures). Will retry automatically."
            })

        try:
            client = self._get_client()
        except Exception as e:
            return tool_error(str(e))

        if tool_name == "m3_profile":
            try:
                memories = client.get_all(user_id=self._user_id, type="user_fact")
                self._record_success()
                if not memories:
                    return json.dumps({"result": "No memories stored yet."})
                lines = [m.get("content", "") for m in memories if m.get("content")]
                return json.dumps({"result": "\n".join(lines), "count": len(lines)})
            except Exception as e:
                self._record_failure()
                return tool_error(f"Failed to fetch profile: {e}")

        elif tool_name == "m3_search":
            query = args.get("query", "")
            if not query:
                return tool_error("Missing required parameter: query")
            top_k = min(int(args.get("top_k", 10)), 50)
            try:
                results = client.search(query=query, user_id=self._user_id, top_k=top_k)
                self._record_success()
                if not results:
                    return json.dumps({"result": "No relevant memories found."})
                items = [{"memory": r.get("content", ""), "score": r.get("score", 0)}
                         for r in results]
                return json.dumps({"results": items, "count": len(items)})
            except Exception as e:
                self._record_failure()
                return tool_error(f"Search failed: {e}")

        elif tool_name == "m3_conclude":
            conclusion = args.get("conclusion", "")
            if not conclusion:
                return tool_error("Missing required parameter: conclusion")
            try:
                client.conclude(content=conclusion, user_id=self._user_id)
                self._record_success()
                return json.dumps({"result": "Fact stored."})
            except Exception as e:
                self._record_failure()
                return tool_error(f"Failed to store: {e}")

        return tool_error(f"Unknown tool: {tool_name}")

    def shutdown(self) -> None:
        for t in (self._prefetch_thread, self._sync_thread):
            if t and t.is_alive():
                t.join(timeout=5.0)
        with self._client_lock:
            self._client = None


def register(ctx) -> None:
    """Register m3 as a memory provider plugin."""
    ctx.register_memory_provider(M3MemoryProvider())
