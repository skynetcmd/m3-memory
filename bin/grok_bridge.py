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
logger = logging.getLogger("grok_intel")

# ── auth_utils import ─────────────────────────────────────────────────────────
# Supports: env var → keyring (Windows Credential Manager / macOS Keychain /
# Linux Secret Service) → macOS Keychain direct → encrypted SQLite vault.
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from m3_sdk import M3Context

ctx = M3Context()

mcp = FastMCP("Grok Intel")

# ── Constants ─────────────────────────────────────────────────────────────────
GROK_URL       = "https://api.x.ai/v1/chat/completions"
GROK_MODEL     = "grok-3"
READ_TIMEOUT   = 30.0   # seconds


# ── Auth ──────────────────────────────────────────────────────────────────────
def _get_xai_key() -> str | None:
    """
    Resolves XAI_API_KEY via auth_utils (cross-platform).
    Resolution order: env var → keyring → macOS Keychain → encrypted vault.
    Token value is never written to logs.
    """
    val = ctx.get_secret("XAI_API_KEY")
    if val:
        logger.info("XAI_API_KEY resolved.")
    return val


# ── Tool ──────────────────────────────────────────────────────────────────────
@mcp.tool()
async def grok_ask(query: str):
    """
    Queries Grok 3 via api.x.ai for real-time X/web data and fast reasoning.
    Uses httpx (not requests/urllib) — required to pass Cloudflare WAF on api.x.ai.
    """
    api_key = _get_xai_key()
    if not api_key:
        logger.error("XAI_API_KEY not found in environment or Keychain.")
        return (
            "Error: XAI_API_KEY not found. "
            "Set it as an env var or store it in macOS Keychain (service: XAI_API_KEY)."
        )

    timeout = httpx.Timeout(connect=5.0, read=READ_TIMEOUT, write=10.0, pool=5.0)

    try:
        logger.info(f"Dispatching to Grok ({GROK_MODEL})...")
        client = ctx.get_async_client()
        response = await client.post(
            GROK_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json={
                "model":    GROK_MODEL,
                "messages": [{"role": "user", "content": query}],
            },
            timeout=timeout,
        )
        response.raise_for_status()

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
        logger.error(f"Read timeout after {READ_TIMEOUT}s waiting for Grok.")
        return f"Error: Grok did not respond within {READ_TIMEOUT}s."
    except httpx.HTTPStatusError as exc:
        logger.error(f"HTTP {exc.response.status_code} from Grok API.")
        return f"Error: Grok returned HTTP {exc.response.status_code}."
    except Exception as exc:
        logger.error(f"Unexpected error: {type(exc).__name__}")
        return f"Error: Unexpected failure ({type(exc).__name__}). Check stderr logs."


if __name__ == "__main__":
    logger.info(f"Grok Intel bridge starting | model={GROK_MODEL}")
    mcp.run()
