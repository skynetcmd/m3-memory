"""Concrete LLM ProseCompiler — the model call behind compile-at-ingest.

Implements the `ProseCompiler` protocol that `compile.py` consumes: given a topic
cluster, produce a compiled markdown page. The pipeline (hashing, min-confidence,
prompt dedupe, supersede-always, idempotence) is already proven with a stub; this
is the one non-deterministic piece, kept injectable and OFF the drift-tested path
for exactly that reason (mirrors synth.py's lede path).

Local-first (§1): calls a local OpenAI-compatible chat endpoint (LM Studio /
llama.cpp / vLLM), the same shape synth.py uses. No cloud dependency; the endpoint
must be reachable without internet. Fail-open: any error → compile() returns None,
and compile.py records "compile-failed" and moves on — a batch never aborts on one
bad topic.

Provenance (plan decision): the FULL prompt text is exposed via prompt_text() so
compile.py can store it as a memory (deduped by content hash) and pin
`compiler.prompt_memory_id`. `prompt_version` bumps whenever the prompt changes,
which — combined with the explicit `--recompile-all` flag — makes a prompt change
an intentional, explained mass-supersede rather than silent churn.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from .cluster import Cluster

# Bump when SYSTEM or the user-prompt template below changes. Combined with the
# stored prompt text, this is what makes a prompt change auditable: a superseded
# synthesis records the exact version AND the exact text it was compiled under.
PROMPT_VERSION = "1"

_SYSTEM = (
    "You are compiling a knowledge-base wiki page from a cluster of related "
    "memory notes. Write a single, self-contained markdown page that SYNTHESIZES "
    "the notes into coherent prose — not a list of the notes, but what they "
    "collectively establish. State only what the notes support; do not invent "
    "facts, and do not overclaim beyond the sources. Where notes disagree, say so "
    "plainly. Be concrete and specific. No preamble like 'Here is the page'; "
    "output the page body directly."
)


@dataclass
class ProseCompilerConfig:
    url: str = "http://127.0.0.1:1234/v1/chat/completions"
    model: str = ""            # empty → let the server pick its loaded model
    api_key_service: str = "LM_API_TOKEN"
    timeout_s: float = 60.0    # a full page is longer than a lede → more headroom
    temperature: float = 0.2
    max_tokens: int = 1200     # a compiled page, not a 2-3 sentence lede
    max_members: int = 40      # cap prompt size; note the truncation if it bites
    snippet_chars: int = 500   # per-member content included in the prompt

    @classmethod
    def from_env(cls) -> "ProseCompilerConfig":
        return cls(
            url=os.environ.get("M3_WIKI_COMPILE_URL",
                               os.environ.get("M3_WIKI_SYNTH_URL", cls.url)),
            model=os.environ.get("M3_WIKI_COMPILE_MODEL",
                                 os.environ.get("M3_WIKI_SYNTH_MODEL", cls.model)),
            timeout_s=float(os.environ.get("M3_WIKI_COMPILE_TIMEOUT", cls.timeout_s)),
        )


def _user_prompt(cluster: Cluster, cfg: ProseCompilerConfig) -> str:
    """Build the user-turn prompt from the cluster's members. Deterministic given
    the same cluster + config, so two runs with an unchanged cluster produce the
    same request (the caller's cluster-hash gate relies on input stability)."""
    topic = cluster.members[0].display_title if cluster.members else "Untitled"
    lines = [f"Topic: {topic}", "", "Source notes:"]
    shown = cluster.members[:cfg.max_members]
    for m in shown:
        body = (m.content or "").strip().replace("\r\n", "\n")
        if len(body) > cfg.snippet_chars:
            body = body[:cfg.snippet_chars] + "…"
        title = m.display_title
        lines.append(f"- [{title}] {body}")
    dropped = len(cluster.members) - len(shown)
    if dropped > 0:
        # Never silently truncate (§3): tell the model (and, via the prompt text,
        # the audit trail) that the cluster was larger than what it saw.
        lines.append("")
        lines.append(f"(NOTE: {dropped} further note(s) omitted to bound prompt size.)")
    lines.append("")
    lines.append("Compile these into one coherent wiki page as instructed.")
    return "\n".join(lines)


def _call_model(cfg: ProseCompilerConfig, user_prompt: str) -> Optional[str]:
    """POST to the local chat endpoint. None on ANY failure (fail-open)."""
    try:
        import httpx  # lazy: only needed when a real compiler is injected
    except ImportError:
        return None

    headers = {"Content-Type": "application/json"}
    token = _resolve_key(cfg.api_key_service)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    payload: dict = {
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "stream": False,
    }
    if cfg.model:
        payload["model"] = cfg.model

    try:
        r = httpx.post(cfg.url, json=payload, headers=headers, timeout=cfg.timeout_s)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        return _clean(text)
    except Exception:
        return None


def _resolve_key(service: str) -> Optional[str]:
    """Resolve an API token via m3's auth_utils (env → keyring → vault). None if
    unavailable — a local endpoint typically needs no auth."""
    if not service:
        return None
    try:
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from auth_utils import get_api_key  # type: ignore
        return get_api_key(service)
    except Exception:
        return None


def _clean(text: str) -> str:
    """Trim whitespace and strip an accidental leading fence/preamble line."""
    t = (text or "").strip()
    if t.startswith("```"):
        # Drop a leading ```lang fence and a trailing ``` if the model wrapped it.
        lines = t.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        t = "\n".join(lines).strip()
    return t


class LLMProseCompiler:
    """The shipped ProseCompiler. Satisfies the protocol compile.py consumes.

    Off the default path: compile.py only invokes a compiler when one is
    injected, so constructing this is an explicit opt-in (like synth's
    --synthesize). Non-deterministic, so never used in the drift test.
    """

    prompt_version = PROMPT_VERSION

    def __init__(self, cfg: Optional[ProseCompilerConfig] = None) -> None:
        self.cfg = cfg or ProseCompilerConfig.from_env()
        self.model = self.cfg.model
        self.calls = 0
        self.failures = 0

    def prompt_text(self) -> str:
        """The audit record of HOW pages are compiled: the system instruction
        plus the user-template shape, stored by compile.py and pinned on every
        synthesis via compiler.prompt_memory_id."""
        return (
            f"[prompt_version={self.prompt_version}]\n\n"
            f"SYSTEM:\n{_SYSTEM}\n\n"
            "USER TEMPLATE:\n"
            "Topic: <topic>\n\nSource notes:\n- [<title>] <content…>\n"
            "(…up to max_members, with an omitted-count note if truncated)\n\n"
            "Compile these into one coherent wiki page as instructed."
        )

    def compile(self, cluster: Cluster) -> Optional[str]:
        self.calls += 1
        prose = _call_model(self.cfg, _user_prompt(cluster, self.cfg))
        if not prose:
            self.failures += 1
            return None
        return prose

    def summary(self) -> str:
        return f"prose-compiler: {self.calls} calls, {self.failures} failed"
