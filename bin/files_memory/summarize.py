"""File + leaf summary generation.

Summarization is intentionally simple in phase 1:
  - File summary: 1-3 sentences, "what is this file about?"
  - Leaf summary: 1-2 sentences, "what is this section about?"
                  Only generated for coarse divisions (page/slide/heading).

Both are written even when the LLM is unavailable. A fallback summarizer
uses the first N chars as a degraded summary so the wiki-index pattern
still works (just less semantically rich). The fallback is clearly
flagged in metadata so a future re-summarization pass can target only
LLM-degraded rows.

Public API:
    summarize_file(text, filename, filetype) -> tuple[str, bool]
    summarize_leaf(text, file_summary=None) -> tuple[str, bool]

Both return (summary_text, used_llm_bool).
"""
from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger("files_memory.summarize")

# Hard cap on input to the summarizer — bge-m3 leaf-sized chunks are fine,
# but a 200-page file's worth of text would blow our token budget. Take
# the head + tail when oversized: opening usually has the topic, tail
# often has the conclusion.
_SUMMARY_INPUT_MAX_CHARS = 8000
_SUMMARY_HEAD_FRACTION = 0.7

# Fallback summary: take first N chars, clean whitespace.
_FALLBACK_MAX_CHARS = 280
_LEAF_FALLBACK_MAX_CHARS = 180

# Prompts. Conservative — we want determinism, not creativity.
_FILE_SUMMARY_PROMPT = (
    "You are summarizing a file so an LLM can decide whether to read its full "
    "contents to answer a question. Output 1 to 3 sentences. Include the file's "
    "topic, filetype, and any standout numbers, dates, or proper nouns. Do not "
    "use bullet points. Do not start with 'This file' or 'The document'."
)

_LEAF_SUMMARY_PROMPT = (
    "You are summarizing one section of a larger document. Output 1 to 2 "
    "sentences naming the section's topic and any standout facts. Be specific. "
    "Do not start with 'This section'."
)


# ──────────────────────────────────────────────────────────────────────────────
# LLM client resolution
# ──────────────────────────────────────────────────────────────────────────────
def _llm_available() -> bool:
    """Probe — is an LLM endpoint configured?

    Phase 1 uses LM Studio's OpenAI-compat endpoint by convention. If
    `M3_FILES_SUMMARY_URL` is set we use that; else if M3_LMSTUDIO_URL is
    set we use it; else we treat the summarizer as unavailable and fall
    back to first-N-chars.
    """
    return bool(_summary_endpoint())


def _summary_endpoint() -> Optional[str]:
    return (
        os.environ.get("M3_FILES_SUMMARY_URL")
        or os.environ.get("M3_LMSTUDIO_URL")
        or None
    )


def _summary_model() -> str:
    return os.environ.get("M3_FILES_SUMMARY_MODEL", "qwen3-4b-instruct")


def _llm_call(prompt: str, content: str, max_tokens: int = 256) -> Optional[str]:
    """Issue a chat completion. Returns None on any failure.

    Synchronous httpx call — the summarizer is invoked per-file/leaf in a
    serial loop by the ingester; no concurrency benefit from async here,
    and we get simpler error handling.
    """
    endpoint = _summary_endpoint()
    if not endpoint:
        return None

    import httpx

    url = endpoint.rstrip("/") + "/v1/chat/completions"
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                url,
                json={
                    "model": _summary_model(),
                    "messages": [
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": content},
                    ],
                    "temperature": 0.0,
                    "max_tokens": max_tokens,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return (data["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        logger.debug("summarizer LLM call failed: %s", e)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _truncate_for_input(text: str) -> str:
    """If text exceeds the summary-input cap, take head + tail.

    The middle is usually less informative than the opening + conclusion
    for summarization. We never feed the full corpus to a single
    summarization call.
    """
    if len(text) <= _SUMMARY_INPUT_MAX_CHARS:
        return text
    head_chars = int(_SUMMARY_INPUT_MAX_CHARS * _SUMMARY_HEAD_FRACTION)
    tail_chars = _SUMMARY_INPUT_MAX_CHARS - head_chars
    return f"{text[:head_chars]}\n\n[...truncated...]\n\n{text[-tail_chars:]}"


def _fallback_summary(text: str, max_chars: int) -> str:
    """Degraded summary: first N chars, whitespace-cleaned."""
    cleaned = " ".join(text.split())  # collapse whitespace, drop newlines
    if len(cleaned) <= max_chars:
        return cleaned
    truncated = cleaned[:max_chars].rsplit(" ", 1)[0]
    return truncated + "..."


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────
def summarize_file(text: str, filename: str, filetype: str) -> tuple[str, bool]:
    """Generate the file-level summary. Returns (summary, used_llm).

    On LLM failure or absence, returns a first-N-chars fallback with
    used_llm=False. The caller stores `used_llm` in metadata so the
    summary can be re-generated later when an LLM is available.
    """
    if not text or not text.strip():
        return (f"Empty {filetype} file: {filename}", False)

    if _llm_available():
        content_for_llm = (
            f"Filename: {filename}\nFiletype: {filetype}\n\n"
            f"Content:\n{_truncate_for_input(text)}"
        )
        result = _llm_call(_FILE_SUMMARY_PROMPT, content_for_llm, max_tokens=160)
        if result:
            return (result, True)

    return (_fallback_summary(text, _FALLBACK_MAX_CHARS), False)


def summarize_leaf(text: str, file_summary: str | None = None) -> tuple[str, bool]:
    """Generate a leaf-level summary. Returns (summary, used_llm).

    The file_summary is passed as context so leaf summaries are framed in
    terms of the parent document. Without it, leaf summaries can read as
    disconnected.
    """
    if not text or not text.strip():
        return ("", False)

    if _llm_available():
        prefix = (
            f"Parent file context: {file_summary}\n\nSection text:\n"
            if file_summary else "Section text:\n"
        )
        content_for_llm = prefix + _truncate_for_input(text)
        result = _llm_call(_LEAF_SUMMARY_PROMPT, content_for_llm, max_tokens=120)
        if result:
            return (result, True)

    return (_fallback_summary(text, _LEAF_FALLBACK_MAX_CHARS), False)
