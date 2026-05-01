"""Unified chat client across Gemini, Claude, and LM Studio.

Hardened httpx client with HTTP/2 disabled and zero keep-alive reuse —
this configuration was provided by Google support to work around
keep-alive hangs we observed against the Gemini OpenAI-compatible
endpoint during high-volume enrichment runs (April-May 2026).

Usage:
    from unified_ai import UnifiedAI
    cli = UnifiedAI(gemini_key=os.environ["GEMINI_API_KEY"])
    text = cli.chat(
        "gemini", "gemini-2.5-flash",
        messages=[{"role": "system", "content": "..."},
                  {"role": "user",   "content": "..."}],
        temperature=0, max_tokens=1024, reasoning_effort="none",
    )

The chat method returns just the assistant's text content. For richer
metadata (token counts, finish reason) call .chat_raw() which returns
the parsed provider-native JSON.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=40.0, write=10.0, pool=10.0)


def _is_gemini_endpoint(url: str | None) -> bool:
    """True for endpoints that need keep-alive disabled to avoid the
    Google OAI-compat hang. Centralized so callers don't sprinkle URL
    matching across the codebase."""
    if not url:
        return False
    return "generativelanguage.googleapis.com" in url


def hardened_async_client(timeout: httpx.Timeout | float | None = None,
                          max_connections: int = 200) -> httpx.AsyncClient:
    """Async httpx.AsyncClient with keep-alive disabled and HTTP/2 off.
    Use ONLY for Gemini's OpenAI-compat endpoint — the rest of our HTTP
    paths (LM Studio local, Anthropic) work fine with stock httpx and
    benefit from connection reuse."""
    if timeout is None:
        timeout = _DEFAULT_TIMEOUT
    return httpx.AsyncClient(
        timeout=timeout,
        http2=False,
        limits=httpx.Limits(max_keepalive_connections=0,
                            max_connections=max_connections),
    )


def async_client_for_profile(profile, *, timeout: float | httpx.Timeout | None = None,
                             max_connections: int = 200) -> httpx.AsyncClient:
    """Pick the right httpx client for a Profile-shaped object.
    Hardened transport only for Gemini; default httpx for everything
    else (LM Studio, Anthropic, OpenAI)."""
    url = getattr(profile, "url", None) if profile is not None else None
    if _is_gemini_endpoint(url):
        return hardened_async_client(timeout=timeout, max_connections=max_connections)
    if timeout is None:
        return httpx.AsyncClient()
    return httpx.AsyncClient(timeout=timeout)


class UnifiedAI:
    def __init__(
        self,
        gemini_key: Optional[str] = None,
        claude_key: Optional[str] = None,
        lmstudio_url: Optional[str] = None,
        timeout: httpx.Timeout | float | None = None,
    ):
        self.gemini_key = gemini_key
        self.claude_key = claude_key
        self.lmstudio_url = lmstudio_url
        if timeout is None:
            timeout = _DEFAULT_TIMEOUT
        # HTTP/2 off + no keep-alive reuse — Google support's recommended
        # workaround for hangs against generativelanguage.googleapis.com.
        self.http = httpx.Client(
            timeout=timeout,
            http2=False,
            limits=httpx.Limits(max_keepalive_connections=0, max_connections=20),
        )

    def close(self) -> None:
        self.http.close()

    def __enter__(self) -> "UnifiedAI":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Unified entry points
    # ------------------------------------------------------------------
    def chat(self, provider: str, model: str, messages: List[Dict], **kwargs) -> str:
        """Return only the assistant's text content."""
        return self.chat_raw(provider, model, messages, **kwargs)["content"]

    def chat_raw(self, provider: str, model: str, messages: List[Dict], **kwargs) -> Dict[str, Any]:
        """Return {'content': str, 'usage': dict, 'raw': dict}."""
        if provider == "gemini":
            return self._chat_gemini(model, messages, **kwargs)
        if provider == "claude":
            return self._chat_claude(model, messages, **kwargs)
        if provider == "lmstudio":
            return self._chat_lmstudio(model, messages, **kwargs)
        raise ValueError(f"Unknown provider: {provider}")

    # ------------------------------------------------------------------
    # Gemini (OpenAI-compatible)
    # ------------------------------------------------------------------
    def _chat_gemini(self, model: str, messages: List[Dict], **kwargs) -> Dict[str, Any]:
        if not self.gemini_key:
            raise RuntimeError("UnifiedAI: gemini_key not configured")
        url = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        payload: Dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        # Pass through any OpenAI-shape parameter (temperature, max_tokens,
        # reasoning_effort, top_p, etc.). Caller is responsible for naming.
        payload.update(kwargs)
        r = self.http.post(
            url,
            headers={"Authorization": f"Bearer {self.gemini_key}"},
            json=payload,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"gemini http {r.status_code}: {r.text[:600]}")
        data = r.json()
        return {
            "content": (data["choices"][0]["message"].get("content") or "").strip(),
            "usage": data.get("usage", {}),
            "raw": data,
        }

    # ------------------------------------------------------------------
    # Claude (Anthropic native)
    # ------------------------------------------------------------------
    def _chat_claude(self, model: str, messages: List[Dict], **kwargs) -> Dict[str, Any]:
        if not self.claude_key:
            raise RuntimeError("UnifiedAI: claude_key not configured")
        url = "https://api.anthropic.com/v1/messages"
        # Anthropic separates 'system' from messages; the OpenAI-style
        # caller may include a system role — pull it out.
        system = None
        anthropic_messages: List[Dict] = []
        for m in messages:
            if m["role"] == "system":
                system = m["content"]
            elif m["role"] in ("user", "assistant"):
                anthropic_messages.append({"role": m["role"], "content": m["content"]})
        payload: Dict[str, Any] = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": kwargs.pop("max_tokens", 2048),
        }
        if system is not None:
            payload["system"] = system
        # Pass through temperature, top_p, etc.
        payload.update(kwargs)
        r = self.http.post(
            url,
            headers={"x-api-key": self.claude_key, "anthropic-version": "2023-06-01"},
            json=payload,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"claude http {r.status_code}: {r.text[:600]}")
        data = r.json()
        text = "".join(
            b.get("text", "") for b in data.get("content", [])
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
        return {"content": text, "usage": data.get("usage", {}), "raw": data}

    # ------------------------------------------------------------------
    # LM Studio (local OpenAI-compatible)
    # ------------------------------------------------------------------
    def _chat_lmstudio(self, model: str, messages: List[Dict], **kwargs) -> Dict[str, Any]:
        if not self.lmstudio_url:
            raise RuntimeError("UnifiedAI: lmstudio_url not configured")
        url = f"{self.lmstudio_url.rstrip('/')}/v1/chat/completions"
        payload: Dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        payload.update(kwargs)
        r = self.http.post(url, json=payload)
        if r.status_code >= 400:
            raise RuntimeError(f"lmstudio http {r.status_code}: {r.text[:600]}")
        data = r.json()
        return {
            "content": (data["choices"][0]["message"].get("content") or "").strip(),
            "usage": data.get("usage", {}),
            "raw": data,
        }
