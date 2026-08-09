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
PROMPT_VERSION = "4"

# PROMPT_VERSION is an AUDIT ANCHOR: every synthesis records the version it was
# compiled under, and a bump is an explicit, traceable recompile event (see the
# plan's supersede design). It only ever increases; the trail to "4" reflects one
# tuning pass (2026-07-24), consolidated here into the rationale for the CURRENT
# prompt rather than a per-version changelog.
#
# Design of this prompt, established by an A/B over a real memory corpus once
# clustering was fixed (so the prompt was the only remaining variable):
#   - LEDE: open with one sentence naming what the page establishes. A plain
#     "synthesize" prompt dove into structure and even renamed a page off its own
#     subject ("MCP lockup" -> "Stability and Architecture").
#   - TOPIC-FIDELITY: stay strictly on subject; exclude off-cluster notes rather
#     than stretching to cover them (the corpus still has mild cross-topic bleed).
#   - NO INVENTED FRAMING: add no causal/narrative connective tissue the notes
#     don't assert (a plain "don't invent facts" did not stop "culminating in…").
#   - PARAPHRASE, DON'T COPY: keep the technical substance but in the model's own
#     words. This is deliberately SOFTER than an earlier "prefer the notes' own
#     specifics" line, which pushed a local 9B to reproduce absolute paths and
#     usernames VERBATIM — the leak that taught the boundary lesson below.
#   - explicit source DELIMITERS live in _user_prompt so the model can't treat its
#     own knowledge as a source.
#
# PII redaction is deliberately NOT in this prompt. The A/B proved a local model
# leaks paths/usernames regardless of instruction (even an explicit "abstract
# these" line failed), so redaction is a SAFETY BOUNDARY, not a best-effort prompt
# (§3 fail-safe, §6 content safety at the boundary). It is enforced in code by
# wiki.pii_scrub over the compiled prose. ASYMMETRY: the SYNTHESIS is the redacted,
# shareable view; the linked SOURCE memories keep full fidelity (paths, hosts,
# usernames) as the system of record, reachable via the consolidates edges.
_SYSTEM = (
    "You are compiling one wiki page from a cluster of related memory notes. "
    "Synthesize what the notes COLLECTIVELY establish into coherent prose — the "
    "connected picture, not a restatement of each note in turn.\n"
    "Begin with a single sentence naming what this page establishes; then develop "
    "the detail.\n"
    "Stay strictly on this page's subject. If a note in the cluster is about "
    "something else, leave it out (a brief 'unrelated notes were excluded' line is "
    "fine) — never stretch the page to cover it.\n"
    "Ground every statement in the notes. Add no causal or narrative connective "
    "tissue the notes do not themselves assert. Keep the technical substance — "
    "which files, versions, commands, and symbols matter — without copying the "
    "notes' text word for word; describe the specifics in your own words.\n"
    "Where notes genuinely disagree, say so. Output the page body directly, with "
    "no preamble."
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
    # Exclude prior syntheses from what the model reads — never feed compiled
    # prose back in as a source (summary-creep compounding). Kept in sync with
    # compile.source_members via a local filter to avoid an import cycle.
    srcs = [m for m in cluster.members if m.type != "synthesis"] or list(cluster.members)
    # Delimit the sources explicitly (v3): a fenced SOURCES block draws a hard line
    # between "material to compile from" and the model's own knowledge, so it can't
    # quietly treat training data as a source. Paraphrased from the field's
    # context-tagging convention, adapted to our per-note format.
    lines = [f"Topic: {topic}", "", "===== SOURCE NOTES (compile ONLY from these) ====="]
    shown = srcs[:cfg.max_members]
    for m in shown:
        body = (m.content or "").strip().replace("\r\n", "\n")
        if len(body) > cfg.snippet_chars:
            body = body[:cfg.snippet_chars] + "…"
        title = m.display_title
        lines.append(f"- [{title}] {body}")
    dropped = len(srcs) - len(shown)
    if dropped > 0:
        # Never silently truncate (§3): tell the model (and, via the prompt text,
        # the audit trail) that the cluster was larger than what it saw.
        lines.append("")
        lines.append(f"(NOTE: {dropped} further note(s) omitted to bound prompt size.)")
    lines.append("===== END SOURCE NOTES =====")
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
    # Turn a reasoning model's thinking OFF so the answer lands in `content`
    # instead of the reasoning channel — otherwise it spends max_tokens thinking
    # and returns an empty string, which this caller reads as "model gave
    # nothing" and silently degrades. Shared seam (slm_intent, m3_entities,
    # files_memory.extract), gated to the local runtimes that honor it.
    try:
        from llm_failover import suppresses_thinking_via_effort
        if suppresses_thinking_via_effort(cfg.url):
            payload["reasoning_effort"] = "none"
    except Exception:  # noqa: BLE001
        pass

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
