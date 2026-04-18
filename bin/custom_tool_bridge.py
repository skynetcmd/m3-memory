import logging
import os
import re
import sqlite3
import sys
from datetime import datetime

import httpx
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_failover import get_best_llm

# ── Logging ───────────────────────────────────────────────────────────────────
# macOS Tahoe: all logs go to stderr. stdout is the MCP stdio transport channel.
# Token values are NEVER logged.
logging.basicConfig(
    level=logging.INFO,
    format='%(name)s: [%(levelname)s] %(message)s',
    stream=sys.stderr,
)
logger = logging.getLogger("m3_max_super_bridge")

mcp = FastMCP("M3 Max Super Bridge")

# ── Constants ─────────────────────────────────────────────────────────────────
PERPLEXITY_URL  = "https://api.perplexity.ai/chat/completions"
PERPLEXITY_MODEL = "sonar-pro"

GROK_URL        = "https://api.x.ai/v1/chat/completions"
GROK_MODEL      = "grok-3-latest"

# LM_MODEL is a fallback when dynamic model selection unavailable
LM_MODEL        = "qwen/qwen3-coder-next"

# DeepSeek-R1 emits a <think> chain before its answer. At ~7.5 tok/s on M3 Max,
# 32768 tokens ≈ 4369 s — 4800 s timeout provides ~10% buffer.
LM_MAX_TOKENS   = 32768
LM_READ_TIMEOUT = 4800.0  # seconds (~80 min for 32k tokens at 7.5 tok/s on M3 Max)
EXT_READ_TIMEOUT = 30.0   # seconds for external APIs (Perplexity, Grok)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "memory", "agent_memory.db")

# ── Auth (delegated to m3_sdk) ────────────────────────────────────────────────
from m3_sdk import M3Context, StructuredLogger
from thermal_utils import get_thermal_status

ctx = M3Context()
sl = StructuredLogger()

# ── Core tools ────────────────────────────────────────────────────────────────

@mcp.tool()
def log_activity(category: str, detail_a: str, detail_b: str, detail_c: str = "None"):
    """
    Routes AI data to the correct agent_memory.db table.
    """
    try:
        # Route to unified SDK logger
        ctx.log_event(category, detail_a, detail_b, detail_c)

        if category == "decision":
            if detail_a.strip():
                with ctx.get_sqlite_conn() as conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "UPDATE memory_items SET importance = 1.0 WHERE (title LIKE ? OR content LIKE ?) AND is_deleted = 0",
                        (f"%{detail_a}%", f"%{detail_b}%")
                    )
                    conn.commit()


        logger.info(sl.format("Activity Logged", category, detail_a=detail_a[:100]))
        return f"Logged to {category} table successfully."

    except sqlite3.OperationalError as exc:
        logger.error(f"DB operational error: {type(exc).__name__}: {exc}")
        return "Error: Database operation failed. Check stderr logs."
    except sqlite3.DatabaseError as exc:
        logger.error(f"DB error: {type(exc).__name__}: {exc}")
        return "Error: Database error. Check stderr logs."
    except Exception as exc:
        logger.error(f"Unexpected DB error: {type(exc).__name__}: {exc}")
        return f"Error: Unexpected failure ({type(exc).__name__}). Check stderr logs."


@mcp.tool()
def update_focus(summary: str):
    """
    Updates the 'Current Focus' ticker for the Pulse dashboard.
    Enforces a single-entry state by using a fixed ID.
    """
    try:
        with ctx.get_sqlite_conn() as conn:
            cursor = conn.cursor()
            # Enforce single entry by deleting anything else first
            cursor.execute("DELETE FROM system_focus WHERE id != 1")
            cursor.execute(
                "INSERT OR REPLACE INTO system_focus (id, summary, timestamp) VALUES (1, ?, ?)",
                (summary, datetime.now().isoformat()),
            )
            conn.commit()
        logger.info(f"System focus updated: {summary}")
        return f"System focus updated to: {summary}"

    except sqlite3.OperationalError as exc:
        logger.error(f"DB operational error: {type(exc).__name__}: {exc}")
        return "Error: Database operation failed. Check stderr logs."
    except sqlite3.DatabaseError as exc:
        logger.error(f"DB error: {type(exc).__name__}: {exc}")
        return "Error: Database error. Check stderr logs."
    except Exception as exc:
        logger.error(f"Unexpected DB error: {type(exc).__name__}: {exc}")
        return f"Error: Unexpected failure ({type(exc).__name__}). Check stderr logs."



@mcp.tool()
def retire_focus():
    """
    Clears the current system focus entry.
    Call this when a task is completed or the focus is no longer active.
    """
    try:
        with ctx.get_sqlite_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM system_focus")
            conn.commit()
        logger.info("System focus retired/cleared.")
        return "System focus has been cleared."

    except sqlite3.OperationalError as exc:
        logger.error(f"DB operational error: {type(exc).__name__}: {exc}")
        return "Error: Database operation failed. Check stderr logs."
    except sqlite3.DatabaseError as exc:
        logger.error(f"DB error: {type(exc).__name__}: {exc}")
        return "Error: Database error. Check stderr logs."
    except Exception as exc:
        logger.error(f"Unexpected error: {type(exc).__name__}: {exc}")
        return f"Error: Unexpected failure ({type(exc).__name__}). Check stderr logs."



@mcp.tool()
def check_thermal_load():
    """
    Checks the system thermal pressure level (Protocol #2).
    Returns one of: Nominal, Fair, Serious, Critical.
    """
    status = get_thermal_status()
    logger.info(f"Thermal check performed: {status}")
    return status


@mcp.tool()
def query_decisions(keyword: str = "", limit: int = 10):
    """
    Searches the project_decisions table for relevant prior decisions.
    Implements Protocol #4 — The Search Rule: call before starting any new task.

    Args:
        keyword: Optional search term matched against project, decision, and rationale.
                 Empty string returns the most recent `limit` decisions.
        limit:   Max number of rows to return (default 10).
    """
    try:
        with ctx.get_sqlite_conn() as conn:
            cursor = conn.cursor()
            if keyword.strip():
                pattern = f"%{keyword.strip()}%"
                cursor.execute(
                    """
                    SELECT project, decision, rationale, timestamp
                    FROM project_decisions
                    WHERE project LIKE ? OR decision LIKE ? OR rationale LIKE ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (pattern, pattern, pattern, limit),
                )
            else:
                cursor.execute(
                    """
                    SELECT project, decision, rationale, timestamp
                    FROM project_decisions
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            rows = cursor.fetchall()

        if not rows:
            return "No prior decisions found."

        lines = []
        for project, decision, rationale, ts in rows:
            lines.append(f"[{ts}] {project}: {decision} | rationale: {rationale}")
        return "\n".join(lines)

    except sqlite3.OperationalError as exc:
        logger.error(f"DB operational error: {type(exc).__name__}")
        return "Error: Database operation failed. Check stderr logs."
    except sqlite3.DatabaseError as exc:
        logger.error(f"DB error: {type(exc).__name__}")
        return "Error: Database error. Check stderr logs."
    except Exception as exc:
        logger.error(f"Unexpected DB error: {type(exc).__name__}")
        return f"Error: Unexpected failure ({type(exc).__name__}). Check stderr logs."



# ── Research tools ────────────────────────────────────────────────────────────

async def _perplexity_search(query: str, api_key: str) -> str:
    """Internal: call Perplexity sonar-pro via centralized SDK helper."""
    response = await ctx.request_with_retry(
        "POST",
        PERPLEXITY_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": PERPLEXITY_MODEL, "messages": [{"role": "user", "content": query}]},
    )
    data = response.json()
    choices = data.get("choices", [])
    if not choices:
        return "Error: Perplexity returned empty choices array."
    content = choices[0].get("message", {}).get("content", "")
    logger.info(f"Perplexity response received | length={len(content)} chars")
    return content


async def _grok_search_fallback(query: str, api_key: str) -> str:
    """Internal: call Grok as research fallback via centralized SDK helper."""
    response = await ctx.request_with_retry(
        "POST",
        GROK_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": GROK_MODEL, "messages": [{"role": "user", "content": query}]},
    )
    data = response.json()
    choices = data.get("choices", [])
    if not choices:
        return "Error: Grok returned empty choices array."
    content = choices[0].get("message", {}).get("content", "")
    logger.info(f"Grok fallback response received | length={len(content)} chars")
    return f"[Research via Grok {GROK_MODEL} — Perplexity unavailable]\n\n{content}"


async def _web_search_with_fallback(query: str) -> str:
    """
    Primary: Perplexity sonar-pro. Fallback: Grok grok-3-latest.
    """
    perplexity_key = ctx.get_secret("PERPLEXITY_API_KEY")

    if perplexity_key:
        try:
            logger.info(f"Dispatching web search to Perplexity ({PERPLEXITY_MODEL})...")
            return await _perplexity_search(query, perplexity_key)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                logger.warning(f"Perplexity auth failed (HTTP {exc.response.status_code}) — falling back to Grok.")
            else:
                logger.error(f"HTTP {exc.response.status_code} from Perplexity.")
                return f"Error: Perplexity returned HTTP {exc.response.status_code}."
        except httpx.ConnectError:
            logger.warning("Perplexity unreachable — falling back to Grok.")
        except httpx.ReadTimeout:
            logger.error(f"Read timeout after {EXT_READ_TIMEOUT}s waiting for Perplexity.")
            return f"Error: Perplexity did not respond within {EXT_READ_TIMEOUT}s."
        except Exception as exc:
            logger.error(f"Unexpected Perplexity error: {type(exc).__name__}")
            return f"Error: Unexpected failure ({type(exc).__name__}). Check stderr logs."
    else:
        logger.warning("PERPLEXITY_API_KEY not found — falling back to Grok.")

    grok_key = ctx.get_secret("GROK_API_KEY")
    if not grok_key:
        return "Error: Neither PERPLEXITY_API_KEY nor GROK_API_KEY available. Add one to env or Keychain."

    try:
        logger.info(f"Dispatching to Grok fallback ({GROK_MODEL})...")
        return await _grok_search_fallback(query, grok_key)
    except httpx.ConnectError:
        return "Error: Both Perplexity and Grok are unreachable."
    except httpx.ReadTimeout:
        return f"Error: Grok did not respond within {EXT_READ_TIMEOUT}s."
    except httpx.HTTPStatusError as exc:
        return f"Error: Grok returned HTTP {exc.response.status_code}."
    except Exception as exc:
        logger.error(f"Unexpected Grok error: {type(exc).__name__}")
        return f"Error: Unexpected failure ({type(exc).__name__}). Check stderr logs."


@mcp.tool()
async def web_search(query: str):
    """
    Searches the live web for current data.
    Primary: Perplexity sonar-pro. Fallback: Grok grok-3-latest (auto, on auth failure or unreachable).
    """
    return await _web_search_with_fallback(query)


@mcp.tool()
async def m3_web_search(query: str):
    """Alias for web_search. Primary: Perplexity sonar-pro. Fallback: Grok grok-3-latest."""
    return await _web_search_with_fallback(query)


@mcp.tool()
async def grok_ask(query: str):
    """Queries Grok 3 for real-time info from X and fast reasoning."""
    api_key = ctx.get_secret("XAI_API_KEY")
    if not api_key:
        logger.error("XAI_API_KEY not found in environment or Keychain.")
        return (
            "Error: XAI_API_KEY not found. "
            "Set it as an env var or store it in macOS Keychain (service: XAI_API_KEY)."
        )

    try:
        logger.info(f"Dispatching to Grok ({GROK_MODEL})...")
        response = await ctx.request_with_retry(
            "POST",
            GROK_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"model": GROK_MODEL, "messages": [{"role": "user", "content": query}]},
        )
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            return "Error: Grok returned empty choices array."
        content = choices[0].get("message", {}).get("content", "")
        logger.info(f"Grok response received | length={len(content)} chars")
        return content

    except httpx.ConnectError:
        logger.error("Cannot connect to api.x.ai.")
        return "Error: Grok API is unreachable. Check your network connection."
    except httpx.ReadTimeout:
        logger.error(f"Read timeout after {EXT_READ_TIMEOUT}s waiting for Grok.")
        return f"Error: Grok did not respond within {EXT_READ_TIMEOUT}s."
    except httpx.HTTPStatusError as exc:
        logger.error(f"HTTP {exc.response.status_code} from Grok API.")
        return f"Error: Grok returned HTTP {exc.response.status_code}."
    except Exception as exc:
        logger.error(f"Unexpected error: {type(exc).__name__}: {exc}")
        return f"Error: Unexpected failure ({type(exc).__name__}). Check stderr logs."


# ── Local logic tool ──────────────────────────────────────────────────────────

@mcp.tool()
async def query_local_model(prompt: str):
    """
    Sends a complex reasoning task to the best available local/network LLM.
    Tries endpoints in failover order: localhost → MacBook Pro → SkyPC → GPU VM.
    Dynamically selects the largest loaded non-embedding model at each endpoint.
    """
    token = ctx.get_secret("LM_API_TOKEN") or "lm-studio"
    client = ctx.get_async_client()

    # Use failover to find best endpoint + model
    result = await get_best_llm(client, token)
    if result is None:
        return (
            "Error: No LLM endpoint reachable. "
            "Start LM Studio (lms server start) or ensure a remote machine is online."
        )

    base_url, target_model = result
    chat_url = f"{base_url}/chat/completions"
    logger.info(f"Using model {target_model!r} @ {base_url}")

    payload = {
        "model":       target_model,
        "messages":    [{"role": "user", "content": prompt}],
        "max_tokens":  LM_MAX_TOKENS,
        "temperature": 0.6,
        "stream":      False,
    }

    try:
        # Use centralized request helper with retry
        response = await ctx.request_with_retry(
            "POST",
            chat_url,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=payload,
        )
    except httpx.ConnectError:
        return f"Error: Lost connection to {base_url} during request."
    except httpx.ReadTimeout:
        return f"Error: {base_url} did not respond within {LM_READ_TIMEOUT}s."
    except httpx.HTTPStatusError as exc:
        return f"Error: {base_url} returned HTTP {exc.response.status_code}."
    except Exception as exc:
        logger.error(f"Unexpected error: {type(exc).__name__}: {exc}")
        return f"Error: Unexpected failure ({type(exc).__name__}). Check stderr logs."

    data          = response.json()
    choices       = data.get("choices", [])
    if not choices:
        return "Error: LLM returned empty choices array."
    choice        = choices[0]
    finish_reason = choice.get("finish_reason", "unknown")
    raw_content   = choice.get("message", {}).get("content", "")

    # Extract answer by stripping <think> tags
    content = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()

    # Protocol #1 — archive substantial reasoning chains
    # Some providers use 'reasoning_content', others put it inside <think> tags in 'content'
    raw_reasoning = choice["message"].get("reasoning_content", "")
    if not raw_reasoning:
        think_match = re.search(r"<think>(.*?)</think>", raw_content, flags=re.DOTALL)
        if think_match:
            raw_reasoning = think_match.group(1).strip()

    if raw_reasoning and len(raw_reasoning) > 200:
        try:
            # Archive with actual model name
            ctx.log_event("thought", prompt[:500], raw_reasoning[:16000], target_model)
            logger.info(f"Reasoning chain archived ({len(raw_reasoning)} chars) for model {target_model}.")
        except Exception as _exc:
            logger.warning(f"Could not archive reasoning chain: {type(_exc).__name__}")

    if not content and raw_reasoning:
        if finish_reason == "length":
             return (
                f"Warning: The model ({target_model}) consumed the full token budget in reasoning. "
                "No final answer was produced. Try a more direct prompt."
            )
        return f"[Reasoning Only Produced]\n\n{raw_reasoning[:2000]}..."

    if not content:
        return "Error: LLM returned an empty response with no reasoning."

    logger.info(
        f"Response received | model={target_model} | endpoint={base_url} | "
        f"finish_reason={finish_reason} | length={len(content)} chars"
    )
    return content


if __name__ == "__main__":
    logger.info(
        f"M3 Max Super Bridge starting | "
        f"local={LM_MODEL} | perplexity={PERPLEXITY_MODEL} | grok={GROK_MODEL}"
    )
    mcp.run()
