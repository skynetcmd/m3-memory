from __future__ import annotations

import logging
import os
import sys

import httpx
from mcp.server.fastmcp import FastMCP

# ── Logging ───────────────────────────────────────────────────────────────────
# All logs go to stderr. stdout is the MCP stdio transport channel.
# Token values are NEVER logged.
logging.basicConfig(
    level=logging.INFO,
    format='%(name)s: [%(levelname)s] %(message)s',
    stream=sys.stderr,
)
logger = logging.getLogger("web_research")

# ── auth_utils import ─────────────────────────────────────────────────────────
# Supports: env var → keyring (Windows Credential Manager / macOS Keychain /
# Linux Secret Service) → macOS Keychain direct → encrypted SQLite vault.
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from m3_sdk import M3Context

ctx = M3Context.for_db(None)

mcp = FastMCP("Web Research")

# ── Constants ─────────────────────────────────────────────────────────────────
PERPLEXITY_URL   = "https://api.perplexity.ai/chat/completions"
PERPLEXITY_MODEL = "sonar-pro"
GROK_URL         = "https://api.x.ai/v1/chat/completions"
GROK_MODEL       = "grok-3-latest"
READ_TIMEOUT     = 30.0   # seconds


# ── Auth ──────────────────────────────────────────────────────────────────────
def _get_perplexity_key() -> str | None:
    """
    Resolves PERPLEXITY_API_KEY via auth_utils (cross-platform).
    Resolution order: env var → keyring → macOS Keychain → encrypted vault.
    """
    val = ctx.get_secret("PERPLEXITY_API_KEY")
    if val:
        logger.info("PERPLEXITY_API_KEY resolved.")
    return val


def _get_grok_key() -> str | None:
    """
    Resolves XAI_API_KEY via auth_utils (cross-platform).
    Resolution order: env var → keyring → macOS Keychain → encrypted vault.
    """
    val = ctx.get_secret("XAI_API_KEY")
    if val:
        logger.info("XAI_API_KEY resolved.")
    return val


# ── Backend callers ────────────────────────────────────────────────────────────
_timeout = httpx.Timeout(connect=5.0, read=READ_TIMEOUT, write=10.0, pool=5.0)


async def _perplexity_search(query: str, api_key: str) -> str:
    response = await ctx.request_with_retry(
        "POST",
        PERPLEXITY_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": PERPLEXITY_MODEL, "messages": [{"role": "user", "content": query}]},
        retries=2
    )
    data = response.json()
    choices = data.get("choices", [])
    if not choices:
        return "Error: Perplexity returned empty choices array."
    content = choices[0].get("message", {}).get("content", "")
    logger.info(f"Perplexity response | length={len(content)} chars")
    return content


async def _grok_search(query: str, api_key: str) -> str:
    response = await ctx.request_with_retry(
        "POST",
        GROK_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"model": GROK_MODEL, "messages": [{"role": "user", "content": query}]},
        retries=2
    )
    data = response.json()
    choices = data.get("choices", [])
    if not choices:
        return "Error: Grok returned empty choices array."
    content = choices[0].get("message", {}).get("content", "")
    logger.info(f"Grok response | length={len(content)} chars")
    return f"[Research via Grok {GROK_MODEL} — Perplexity unavailable]\n\n{content}"


# ── Tool ──────────────────────────────────────────────────────────────────────
@mcp.tool()
async def web_search(query: str):
    """
    Searches the live web for current data.
    Primary: Perplexity sonar-pro (grounded, cited).
    Fallback: Grok grok-3-latest — used automatically when Perplexity key is
    missing or the API returns 401/403/unreachable.
    """
    perplexity_key = _get_perplexity_key()

    # ── Try Perplexity first ──────────────────────────────────────────────────
    if perplexity_key:
        try:
            logger.info(f"Dispatching to Perplexity ({PERPLEXITY_MODEL})...")
            return await _perplexity_search(query, perplexity_key)

        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in (401, 403):
                logger.warning(f"Perplexity auth failed (HTTP {status}) — falling back to Grok.")
            else:
                logger.error(f"HTTP {status} from Perplexity.")
                return f"Error: Perplexity returned HTTP {status}."

        except httpx.ConnectError:
            logger.warning("Perplexity unreachable — falling back to Grok.")

        except httpx.ReadTimeout:
            logger.error(f"Read timeout after {READ_TIMEOUT}s waiting for Perplexity.")
            return f"Error: Perplexity did not respond within {READ_TIMEOUT}s."

        except Exception as exc:
            logger.error(f"Unexpected Perplexity error: {type(exc).__name__}")
            return f"Error: Unexpected failure ({type(exc).__name__}). Check stderr logs."

    else:
        logger.warning("PERPLEXITY_API_KEY not found — falling back to Grok.")

    # ── Grok fallback ─────────────────────────────────────────────────────────
    grok_key = _get_grok_key()
    if not grok_key:
        logger.error("XAI_API_KEY not found — no research backend available.")
        return (
            "Error: Neither PERPLEXITY_API_KEY nor XAI_API_KEY is available. "
            "Add one to env or macOS Keychain."
        )

    try:
        logger.info(f"Dispatching to Grok fallback ({GROK_MODEL})...")
        return await _grok_search(query, grok_key)

    except httpx.ConnectError:
        logger.error("Cannot connect to api.x.ai.")
        return "Error: Both Perplexity and Grok are unreachable."
    except httpx.ReadTimeout:
        logger.error(f"Read timeout after {READ_TIMEOUT}s waiting for Grok.")
        return f"Error: Grok did not respond within {READ_TIMEOUT}s."
    except httpx.HTTPStatusError as exc:
        logger.error(f"HTTP {exc.response.status_code} from Grok.")
        return f"Error: Grok returned HTTP {exc.response.status_code}."
    except Exception as exc:
        logger.error(f"Unexpected Grok error: {type(exc).__name__}")
        return f"Error: Unexpected failure ({type(exc).__name__}). Check stderr logs."


if __name__ == "__main__":
    logger.info(f"Web Research bridge starting | primary={PERPLEXITY_MODEL} fallback={GROK_MODEL}")
    mcp.run()
