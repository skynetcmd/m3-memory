"""Debug Agent MCP Bridge — Autonomous debugging tools.

Tools: debug_analyze, debug_bisect, debug_trace, debug_correlate, debug_history, debug_report

Registration (settings.json):
  "debug_agent": {
      "command": "python3",
      "args": ["[M3_MEMORY_ROOT]/bin/debug_agent_bridge.py"]
  }

All internal paths are relative to BASE_DIR (auto-detected or AI_WORKSPACE_DIR env var).
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from embedding_utils import (
    parse_model_size as _parse_model_size,
)
from m3_sdk import LM_STUDIO_BASE, M3Context
from thermal_utils import get_thermal_status

ctx = M3Context()
sl = ctx.get_logger()

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(name)s: [%(levelname)s] %(message)s',
    stream=sys.stderr,
)
logger = logging.getLogger("debug_agent")

mcp = FastMCP("Debug Agent Bridge")

# ── Constants ─────────────────────────────────────────────────────────────────
PYTHON_CMD = "python" if os.name == "nt" else "python3"
BASE_DIR = os.environ.get(
    "AI_WORKSPACE_DIR",
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
)
DB_PATH = os.path.join(BASE_DIR, "memory", "agent_memory.db")

LM_STUDIO_URL        = f"{LM_STUDIO_BASE}/chat/completions"
LM_STUDIO_MODELS_URL = f"{LM_STUDIO_BASE}/models"
LM_STUDIO_EMBED_URL  = f"{LM_STUDIO_BASE}/embeddings"
LM_MAX_TOKENS        = 32768
LM_READ_TIMEOUT      = 4800.0   # ~80 min for 32k tokens at 7.5 tok/s on M3 Max
MAX_CONTEXT_CHARS    = 60_000   # ~15k tokens
ORIGIN_DEVICE        = os.environ.get("ORIGIN_DEVICE", platform.node())

# ── DB helpers ────────────────────────────────────────────────────────────────
from contextlib import contextmanager


@contextmanager
def _conn():
    with ctx.get_sqlite_conn() as c:
        c.row_factory = sqlite3.Row
        yield c

# ── Embedding helpers ─────────────────────────────────────────────────────────
_ACTIVE_EMBED_MODEL = None
_EMBED_MODEL_LOCK = asyncio.Lock()

async def _select_embedding_model(models: list[str]) -> str:
    """Select embedding model: prefer dedicated embedding models, fallback to LLMs."""
    dedicated = [m for m in models if any(k in m.lower() for k in ("nomic", "e5", "gte", "bge", "minilm"))]
    if dedicated:
        return min(dedicated, key=_parse_model_size)
    return models[0] if models else "text-embedding-nomic-embed-text-v1.5"

async def _embed(text: str) -> tuple[list[float] | None, str]:
    """Generate embedding via LM Studio with dynamic model selection (Architecture #1)."""
    global _ACTIVE_EMBED_MODEL
    token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
    model_to_use = _ACTIVE_EMBED_MODEL or "text-embedding-nomic-embed-text-v1.5"

    try:
        if not _ACTIVE_EMBED_MODEL:
            async with _EMBED_MODEL_LOCK:
                if not _ACTIVE_EMBED_MODEL:
                    try:
                        m_resp = await ctx.request_with_retry(
                            "GET",
                            LM_STUDIO_MODELS_URL,
                            headers={"Authorization": f"Bearer {token}"},
                            retries=1
                        )
                        models = [m["id"] for m in m_resp.json().get("data", [])]
                        if models:
                            model_to_use = await _select_embedding_model(models)
                            _ACTIVE_EMBED_MODEL = model_to_use
                            logger.info(f"Embedding model selected: {model_to_use}")
                    except Exception as exc:
                        logger.debug(f"Model auto-detect failed: {type(exc).__name__}")
            model_to_use = _ACTIVE_EMBED_MODEL or model_to_use

        resp = await ctx.request_with_retry(
            "POST",
            LM_STUDIO_EMBED_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"model": model_to_use, "input": text},
            retries=2
        )
        result = resp.json()
        if "data" in result and len(result["data"]) > 0:
            embedding = result["data"][0].get("embedding")
            if embedding:
                return embedding, model_to_use
        return None, model_to_use
    except Exception as exc:
        logger.error(f"Embed failed: {type(exc).__name__}")
    return None, model_to_use

def _queue_chroma(memory_id: str, operation: str) -> None:
    """Queue item for ChromaDB sync."""
    try:
        with ctx.get_sqlite_conn() as db:
            db.execute("INSERT INTO chroma_sync_queue (memory_id, operation) VALUES (?,?)", (memory_id, operation))
    except Exception as exc:
        logger.warning(f"chroma queue insert failed: {exc}")

# ── Thermal check ─────────────────────────────────────────────────────────────
def _check_thermal() -> str:
    return get_thermal_status()

# ── LLM helpers ───────────────────────────────────────────────────────────────
async def _get_largest_llm_model() -> str:
    """Query /v1/models, filter out embedding models, return largest by param count."""
    token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
    try:
        resp = await ctx.request_with_retry(
            "GET",
            LM_STUDIO_MODELS_URL,
            headers={"Authorization": f"Bearer {token}"},
            retries=2
        )
        loaded_models = resp.json().get("data", [])
        text_models = []
        for m in loaded_models:
            mid = m.get("id", "")
            if not mid or any(k in mid.lower() for k in ("embed", "nomic", "jina", "bge", "e5", "gte", "minilm")):
                continue
            text_models.append((_parse_model_size(mid), mid))

        if text_models:
            text_models.sort(key=lambda x: x[0], reverse=True)
            return text_models[0][1]
        return loaded_models[0].get("id", "") if loaded_models else "unknown"
    except Exception:
        return "Error: LM Studio not available"

async def _query_llm(prompt: str, max_tokens: int = LM_MAX_TOKENS) -> str:
    """Send prompt to largest loaded LLM with thermal awareness."""
    thermal = _check_thermal()
    if thermal in ("Serious", "Critical"):
        max_tokens = min(max_tokens, 8192)
        _log_to_db("hardware", "thermal_pressure", thermal)

    model = await _get_largest_llm_model()
    if model.startswith("Error:"): return model

    token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
    payload = {
        "model":       model,
        "messages":    [{"role": "user", "content": prompt}],
        "max_tokens":  max_tokens,
        "temperature": 0.2,
        "stream":      False,
    }

    try:
        response = await ctx.request_with_retry(
            "POST",
            LM_STUDIO_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
            retries=3
        )
        data = response.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    except Exception as exc:
        return f"Error: LLM query failed: {type(exc).__name__}"

# ── File helpers ──────────────────────────────────────────────────────────────
def _safe_read_file(file_path: str) -> str:
    """Read file with path resolution, traversal guard, and truncation."""
    if not os.path.isabs(file_path):
        file_path = os.path.join(BASE_DIR, file_path)
    file_path = os.path.normpath(os.path.realpath(file_path))
    safe_base = os.path.join(os.path.realpath(BASE_DIR), "")
    if not file_path.startswith(safe_base):
        return "Error: Access denied — path outside workspace."
    if not os.path.exists(file_path):
        return f"Error: File not found: {file_path}"
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read(MAX_CONTEXT_CHARS)
        return content + ("\n\n[... truncated ...]" if len(content) == MAX_CONTEXT_CHARS else "")
    except Exception as exc:
        return f"Error reading file: {type(exc).__name__}"

def _find_callers(function_name: str) -> str:
    """Platform-agnostic caller discovery (H8)."""
    callers = []
    try:
        for root, _, files in os.walk(BASE_DIR):
            for file in files:
                if file.endswith(".py"):
                    path = os.path.join(root, file)
                    try:
                        with open(path, "r", encoding="utf-8", errors="ignore") as f:
                            for i, line in enumerate(f, 1):
                                if function_name in line and f"def {function_name}" not in line:
                                    callers.append(f"{os.path.relpath(path, BASE_DIR)}:{i}: {line.strip()}")
                                    if len(callers) > 20: break
                    except Exception: continue
                if len(callers) > 20: break
        return "\n## Callers\n" + "\n".join(callers) if callers else ""
    except Exception as e:
        return f"\n## Callers\n[Search failed: {e}]"

# ── Utility ───────────────────────────────────────────────────────────────────
def _log_to_db(category: str, detail_a: str, detail_b: str):
    """Log to agent_memory.db via SDK."""
    try:
        ctx.log_event(category, detail_a, detail_b)
    except Exception as e:
        logger.debug(f"DB log failed: {e}")

# ── Tool implementations ──────────────────────────────────────────────────────
@mcp.tool()
async def debug_analyze(error_message: str, context: str = "", file_path: str = ""):
    """Root cause analysis with memory-augmented reasoning."""
    source_content = _safe_read_file(file_path) if file_path else ""
    prompt = f"ERROR: {error_message}\nCONTEXT: {context}\nSOURCE:\n{source_content}\nAnalyze root cause."
    return await _query_llm(prompt)

@mcp.tool()
async def debug_trace(file_path: str, function_name: str = "", error_type: str = ""):
    """Execution flow analysis — reads source, finds callers."""
    content = _safe_read_file(file_path)
    callers = _find_callers(function_name) if function_name else ""
    return f"SOURCE:\n{content}\n{callers}"

@mcp.tool()
async def debug_bisect(test_command: str, good_commit: str, bad_commit: str = "HEAD"):
    """Automated git bisect placeholder."""
    return "Error: git-bisect tool requires local environment interactive shell."

@mcp.tool()
async def debug_correlate(log_file: str = "", time_range: str = "24h", pattern: str = ""):
    """Cross-reference logs and decisions."""
    content = _safe_read_file(log_file) if log_file else "No log file provided."
    return f"LOGS ({time_range}):\n{content[:2000]}"

@mcp.tool()
def debug_history(keyword: str = "", limit: int = 10):
    """
    Search past debugging sessions.
    Efficiency: Uses hybrid relevance logic if keyword is provided.
    """
    if keyword.strip():
        # Efficiency Suggestion #2: Simple ranking logic
        query = """
            SELECT title, findings
            FROM debug_reports
            WHERE title LIKE ? OR findings LIKE ?
            ORDER BY (CASE WHEN title LIKE ? THEN 2 ELSE 0 END + CASE WHEN findings LIKE ? THEN 1 ELSE 0 END) DESC
            LIMIT ?
        """
        pattern = f"%{keyword.strip()}%"
        rows = ctx.query_memory(query, (pattern, pattern, pattern, pattern, limit))
    else:
        rows = ctx.query_memory("SELECT title, findings FROM debug_reports ORDER BY created_at DESC LIMIT ?", (limit,))

    sl.log("History Queried", "audit", keyword=keyword, results=len(rows))
    return "\n".join([f"- {r[0]}: {r[1]}" for r in rows]) if rows else "No history found."

@mcp.tool()
def debug_report(title: str, findings: str, issue_id: str = ""):
    """Generate and persist a structured debugging report."""
    rid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    try:
        with ctx.get_sqlite_conn() as db:
            db.execute("CREATE TABLE IF NOT EXISTS debug_reports (id TEXT PRIMARY KEY, title TEXT, findings TEXT, issue_id TEXT, created_at TEXT)")
            db.execute("INSERT INTO debug_reports VALUES (?,?,?,?,?)", (rid, title, findings, issue_id, now))
        sl.log("Report Saved", "audit", report_id=rid, title=title)
        return f"Report saved: {rid}"
    except Exception as e:
        sl.log("Report Failed", "error", error=str(e))
        return f"Error saving report: {e}"

if __name__ == "__main__":
    logger.info(f"Debug Agent Bridge starting | DB={DB_PATH}")
    mcp.run()
