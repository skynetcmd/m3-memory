"""Doctor probe: can file fact-extraction reach an LLM?

Inline/queue file-extraction (`m3 files files_ingest --extract_mode inline|queue`,
`files_extract_pending`) needs a chat/instruct LLM. The endpoint is resolved by
`files_memory.extract._llm_endpoint()`, which — after the seam fix — falls back to
the SHARED `llm_failover` resolution (auto-probes LM Studio :1234) instead of only
its own optional `M3_FILES_*` env vars.

This probe surfaces the failure that was previously silent: file-extraction
reporting "no LLM" (leaves marked extraction_status='failed') even though the rest
of m3 (cognitive loop, enrichment) reaches an LLM fine. It reads only config/env
+ a couple of HTTP calls (never the live DB), and never crashes the doctor.

It ALSO warns when the loaded model is a REASONING model with thinking on — the
answer goes to the model's reasoning channel instead of JSON content, so
extraction is slow and often yields 0 facts. Detection is by OUTPUT (an active
one-shot probe), not the model name, and works against any OpenAI-compatible
server — LM Studio AND Ollama — so the readiness check is provider-agnostic.
"""
from __future__ import annotations

import os
import sys
import urllib.request
from urllib.parse import urlparse


def _payload_bin() -> str:
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _ensure_path() -> None:
    b = _payload_bin()
    if b not in sys.path:
        sys.path.insert(0, b)


def _file_extract_endpoint() -> "str | None":
    try:
        _ensure_path()
        from files_memory.extract import _llm_endpoint
        return _llm_endpoint()
    except Exception:  # noqa: BLE001 — diagnostic best-effort
        return None


def _main_path_endpoints() -> list[str]:
    try:
        _ensure_path()
        from llm_failover import LLM_ENDPOINTS
        return list(LLM_ENDPOINTS or [])
    except Exception:  # noqa: BLE001
        return []


def _reach(endpoint: str, timeout: float = 3.0) -> "tuple[str, list[str]]":
    """Return (state, models). state in {'ok','auth','down','bad-scheme'}."""
    if urlparse(endpoint).scheme not in ("http", "https"):
        return "bad-scheme", []
    try:
        _ensure_path()
        from files_memory.config import llm_auth_headers
        headers = llm_auth_headers()
    except Exception:  # noqa: BLE001
        headers = {}
    req = urllib.request.Request(f"{endpoint.rstrip('/')}/models", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 — scheme checked
            import json
            body = json.loads(r.read().decode("utf-8", "replace"))
        models = [m.get("id", "") for m in body.get("data", []) if isinstance(m, dict)]
        return "ok", [m for m in models if m]
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return ("auth" if e.code in (401, 403) else "down"), []
    except Exception:  # noqa: BLE001
        return "down", []


def _classify_thinking(body: dict) -> "str | None":
    """From a /chat/completions response, is the model doing chain-of-thought?

    'on'  — the reply carries reasoning: LM Studio's ``reasoning_content``,
            Ollama's ``thinking``/``reasoning``, or an inline ``<think>`` block.
    'off' — it answered with plain content and no reasoning channel.
    None  — unrecognizable shape.

    Name heuristics don't work here (gemma-4 / qwen3.5 don't advertise "reasoning"
    in the id), so detection is by OUTPUT. Pure — testable without a server."""
    try:
        msg = (body.get("choices") or [{}])[0].get("message") or {}
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(msg, dict):
        return None
    reasoning = str(msg.get("reasoning_content") or msg.get("reasoning")
                    or msg.get("thinking") or "").strip()
    content = str(msg.get("content") or "")
    if reasoning or "<think>" in content.lower():
        return "on"
    return "off"


def _detect_thinking(endpoint: str, model: str, headers: dict, timeout: float = 20.0) -> "str | None":
    """Active probe: send ONE tiny chat request and classify whether the loaded
    model reasons. Works against any OpenAI-compatible server (LM Studio, Ollama,
    vLLM, …) at ``{endpoint}/chat/completions``. Returns 'on'|'off'|None; None on
    any error (never a false alarm)."""
    import json
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": 200,
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{endpoint.rstrip('/')}/chat/completions", data=payload,
        headers={**headers, "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310 — scheme checked upstream
            return _classify_thinking(json.loads(r.read().decode("utf-8", "replace")))
    except Exception:  # noqa: BLE001 — diagnostic best-effort
        return None


def run(brief: bool = False, fix: bool = False) -> int:
    endpoint = _file_extract_endpoint()
    main_eps = _main_path_endpoints()

    # Regression guard: file-extraction blind while the main path has an LLM.
    if not endpoint:
        if main_eps:
            print("❌ file-extraction LLM: NONE resolved, but the main path has "
                  f"{main_eps[0]} — inline/queue extraction will no-op (leaves "
                  "marked 'failed').")
            print("   fix: file-extraction should fall back to llm_failover "
                  "(bin/files_memory/extract.py::_llm_endpoint).")
            return 1
        print("file-extraction LLM: none configured (no LLM anywhere) — fact "
              "extraction is off; embeddings + keyword search still work.")
        return 0

    if brief:
        return 0

    state, models = _reach(endpoint)
    if state == "ok":
        shown = ", ".join(models[:3]) if models else "no models loaded"
        glyph = "✅" if models else "⚠️ "
        print(f"{glyph} file-extraction LLM: {endpoint} reachable ({shown})"
              + ("" if models else " — load a chat/instruct model to enable extraction."))
        if models:
            # Reasoning/thinking ON = extraction is slow and often yields 0
            # parseable facts (the answer goes to the reasoning channel, not the
            # JSON content). Detect it live (works for LM Studio AND Ollama) and
            # warn — a model choice, not an m3 defect, so it doesn't fail the exit.
            _ensure_path()
            try:
                from files_memory.config import llm_auth_headers
                _hdr = llm_auth_headers()
            except Exception:  # noqa: BLE001
                _hdr = {}
            if _detect_thinking(endpoint, models[0], _hdr) == "on":
                print(f"⚠️  reasoning/thinking is ON for '{models[0]}' — fact "
                      "extraction (files + entities) will be SLOW and may yield 0 "
                      "parseable facts.")
                print("   fix: disable thinking for this model in LM Studio / Ollama, "
                      "or load a non-reasoning instruct model.")
        return 0 if models else 1
    if state == "auth":
        print(f"⚠️  file-extraction LLM: {endpoint} needs auth and the token didn't "
              "resolve — set LM_API_TOKEN in the OS keyring (headless-safe).")
        return 1
    print(f"⚠️  file-extraction LLM: {endpoint} not reachable — inline/queue "
          "extraction will no-op until it serves.")
    return 1
