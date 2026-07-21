"""Optional prose synthesis for topic pages.

Opt-in (`m3 wiki generate --synthesize`). For each topic cluster, asks a local
OpenAI-compatible chat endpoint to write a 2-3 sentence lede summarizing what the
cluster is about. Everything here degrades gracefully:

  - no endpoint / model / network error  → returns None, page keeps its
    deterministic member-list body (generation never fails).
  - a lede is CACHED on disk keyed by a content-hash of the exact inputs
    (member ids + titles + snippets + prompt/model). An unchanged cluster is
    never re-summarized, so repeated `--synthesize` runs are cheap AND the
    output is stable enough for the same-inputs → same-output contract.

Determinism note: LLM output is not bit-reproducible, so synthesized ledes are
NOT part of the drift-tested surface. The cache is what makes a re-run stable in
practice; `--check` should be run WITHOUT `--synthesize` (the deterministic vault
is the drift-gated artifact).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Optional

from .cluster import Cluster

_PROMPT_VERSION = "1"  # bump to invalidate all caches when the prompt changes

_SYSTEM = (
    "You summarize a cluster of related knowledge-base notes into a short lede for "
    "a wiki page. Write 2-3 plain sentences describing what this topic is about and "
    "why it matters. No preamble, no bullet points, no markdown headings — just the "
    "prose. Be concrete and specific to the notes; do not invent facts."
)


@dataclass
class SynthConfig:
    url: str = "http://127.0.0.1:1234/v1/chat/completions"
    model: str = ""            # empty → let the server pick its loaded model
    api_key_service: str = "LM_API_TOKEN"
    timeout_s: float = 30.0
    temperature: float = 0.2
    max_tokens: int = 220
    cache_dir: Optional[str] = None   # where ledes are cached (None → no cache)

    @classmethod
    def from_env(cls, cache_dir: Optional[str]) -> "SynthConfig":
        return cls(
            url=os.environ.get("M3_WIKI_SYNTH_URL", cls.url),
            model=os.environ.get("M3_WIKI_SYNTH_MODEL", cls.model),
            timeout_s=float(os.environ.get("M3_WIKI_SYNTH_TIMEOUT", cls.timeout_s)),
            cache_dir=cache_dir,
        )


def _cluster_hash(c: Cluster, model: str) -> str:
    h = hashlib.sha256()
    h.update(_PROMPT_VERSION.encode())
    h.update((model or "").encode())
    for m in c.members:  # members are already deterministically ordered
        head = (m.content or "").strip().splitlines()
        snippet = head[0].strip() if head else ""
        h.update(m.id.encode())
        h.update((m.display_title or "").encode())
        h.update(snippet[:300].encode())
    return h.hexdigest()[:16]


def _cache_path(cfg: SynthConfig, digest: str) -> Optional[str]:
    if not cfg.cache_dir:
        return None
    return os.path.join(cfg.cache_dir, f"{digest}.json")


def _read_cache(path: Optional[str]) -> Optional[str]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("lede")
    except Exception:
        return None


def _write_cache(path: Optional[str], digest: str, lede: str) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump({"hash": digest, "lede": lede}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # a cache write failure must never break generation


def _build_user_prompt(c: Cluster) -> str:
    lines = [f"Topic: {c.members[0].display_title}", "", "Notes in this cluster:"]
    for m in c.members[:20]:
        head = (m.content or "").strip().splitlines()
        snippet = head[0].strip() if head else ""
        snippet = (snippet[:200] + "…") if len(snippet) > 200 else snippet
        lines.append(f"- {m.display_title}: {snippet}")
    return "\n".join(lines)


def _call_model(cfg: SynthConfig, user_prompt: str) -> Optional[str]:
    try:
        import httpx  # lazy: only needed when --synthesize is used
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
        data = r.json()
        text = data["choices"][0]["message"]["content"]
        return _clean(text)
    except Exception:
        return None


def _resolve_key(service: str) -> Optional[str]:
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
    t = (text or "").strip()
    # Strip accidental markdown headings / leading bullet.
    t = t.lstrip("#").lstrip("-").strip()
    return t


class Synthesizer:
    """Produces (and caches) per-cluster ledes. Reports availability once."""

    def __init__(self, cfg: SynthConfig) -> None:
        self.cfg = cfg
        self.calls = 0
        self.cache_hits = 0
        self.failures = 0

    def lede_for(self, c: Cluster) -> Optional[str]:
        digest = _cluster_hash(c, self.cfg.model)
        path = _cache_path(self.cfg, digest)
        cached = _read_cache(path)
        if cached is not None:
            self.cache_hits += 1
            return cached
        prose = _call_model(self.cfg, _build_user_prompt(c))
        self.calls += 1
        if not prose:
            self.failures += 1
            return None
        _write_cache(path, digest, prose)
        return prose

    def summary(self) -> str:
        return (f"synthesis: {self.calls} generated, {self.cache_hits} cached, "
                f"{self.failures} failed")
