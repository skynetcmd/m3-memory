"""Small-Language-Model intent classifier with named-profile configs.

One compact LLM call that maps a user query (or other short text) to a
label from a fixed set — used by intent-aware retrieval, chatlog
triage, and benchmark harness routing. Each call site picks a
**profile** by name, and each profile is a YAML file pinning its own
endpoint URL, model, prompt, label vocabulary, and timeout.

Why profiles (vs. a single global config):
  - Bench harness wants a prompt tuned for LongMemEval categories.
  - Chatlog triage wants a different label set (sensitive / routine /
    administrative) and probably a faster model.
  - General memory routing wants a middle-ground prompt.
Profiles let each subsystem iterate on its own prompt file without
touching the others.

Resolution order for profile **content**:
  1. ``classify_intent(profile=...)`` kwarg
  2. ``M3_SLM_PROFILE`` env var
  3. ``"default"`` (must exist in one of the profile dirs)

Resolution order for profile **file location** (first match wins):
  1. ``M3_SLM_PROFILES_DIR`` env var — may be a single path OR a
     ``os.pathsep``-separated list (e.g. for bench harnesses that
     want to stack their own dir ahead of the repo default).
  2. ``<M3_MEMORY_ROOT>/config/slm/``

Gate: ``M3_SLM_CLASSIFIER={1|true|yes}``. When off, ``classify_intent``
returns ``None`` immediately — callers should treat that as "no intent
signal available, fall through to heuristics."

Config YAML shape::

    url: http://127.0.0.1:11434/v1/chat/completions
    model: qwen2.5:1.5b-instruct
    api_key_service: LM_API_TOKEN   # optional; looked up via auth_utils
    timeout_s: 10.0
    temperature: 0
    system: |
      <system prompt>
    labels:
      - label-one
      - label-two
    fallback: label-one              # returned when model output matches no label

Profiles are cached by name once loaded; call ``invalidate_cache()``
after editing a YAML for the change to take effect in a long-running
process.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

try:
    import yaml
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "slm_intent requires PyYAML. Install via `pip install PyYAML` "
        "or reinstall m3-memory with its declared deps."
    ) from e

logger = logging.getLogger("slm_intent")


# ── Gate + env-driven resolution ──────────────────────────────────────────────
def _gate_on() -> bool:
    return os.environ.get("M3_SLM_CLASSIFIER", "").strip().lower() in ("1", "true", "yes")


def _default_profile_name() -> str:
    return os.environ.get("M3_SLM_PROFILE", "").strip() or "default"


def _profile_search_dirs() -> list[Path]:
    """Ordered list of directories to search for <profile>.yaml.

    ``M3_SLM_PROFILES_DIR`` may contain multiple paths separated by the
    platform path separator (``;`` on Windows, ``:`` elsewhere) so a bench
    harness can prepend its own dir. The repo-root fallback always comes
    last so a missing override never surprises the caller with a hard fail.
    """
    dirs: list[Path] = []
    env = os.environ.get("M3_SLM_PROFILES_DIR", "").strip()
    if env:
        for part in env.split(os.pathsep):
            p = part.strip()
            if p:
                dirs.append(Path(p))
    # Repo-root default. M3_MEMORY_ROOT is set by m3_sdk at process start;
    # fall back to bin/'s parent when that env var isn't present yet.
    base = os.environ.get("M3_MEMORY_ROOT") or str(Path(__file__).resolve().parent.parent)
    dirs.append(Path(base) / "config" / "slm")
    return dirs


# ── Profile dataclass + loader + cache ────────────────────────────────────────
@dataclass(frozen=True)
class Profile:
    name: str
    url: str
    model: str
    system: str
    labels: tuple[str, ...]
    fallback: str
    temperature: float
    timeout_s: float
    api_key_service: Optional[str]
    # Wire-format backend. "openai" (default) sends an OpenAI-compatible
    # /v1/chat/completions body and reads `choices[0].message.content` —
    # works against Ollama, LM Studio default, llama-server, vLLM,
    # OpenAI itself. "anthropic" sends Anthropic's /v1/messages body
    # and reads `content[0].text` — works against Anthropic's cloud and
    # any local server that serves the same format (LM Studio 0.3+).
    backend: str = "openai"
    # Anthropic-only: wrap the system prompt in a cache_control ephemeral
    # block so Anthropic cloud charges the system prompt once and reads
    # cached on subsequent calls. LM Studio accepts the field and
    # silently ignores it. No effect when backend=="openai".
    cache_system: bool = True
    # Anthropic-only: API version header sent with every request.
    anthropic_version: str = "2023-06-01"
    # Post-processing for free-text output (extract_text / extract_entities).
    # All optional. Applied in order: skip_if_matches → strip_prefixes →
    # format. classify_intent does NOT apply these — its label-matching
    # picks a discrete label so prose cleanup is not needed.
    post_strip_prefixes: tuple[str, ...] = ()  # regex patterns stripped from output start
    post_skip_if_matches: tuple[str, ...] = ()  # regex patterns that mark the output as empty
    post_format: Optional[str] = None  # optional "{text}"-style wrapper


_PROFILE_CACHE: dict[str, Profile] = {}
_PROFILE_CACHE_LOCK = threading.Lock()


def invalidate_cache() -> None:
    """Drop cached profiles so the next classify_intent() re-reads YAMLs."""
    with _PROFILE_CACHE_LOCK:
        _PROFILE_CACHE.clear()


def _find_profile_file(name: str) -> Optional[Path]:
    for d in _profile_search_dirs():
        candidate = d / f"{name}.yaml"
        if candidate.is_file():
            return candidate
    return None


def _parse_profile(name: str, path: Path) -> Profile:
    """Load and validate a profile YAML. Raises ValueError on malformed content."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: profile must be a YAML mapping")
    missing = [k for k in ("url", "model", "system", "labels") if k not in raw]
    if missing:
        raise ValueError(f"{path}: profile missing required keys: {missing}")
    labels = raw["labels"]
    if not isinstance(labels, list) or not labels:
        raise ValueError(f"{path}: labels must be a non-empty list")
    fallback = raw.get("fallback") or labels[0]
    if fallback not in labels:
        raise ValueError(f"{path}: fallback {fallback!r} not in labels {labels}")
    # Optional post-processing block. Each field defaults to absent; when
    # present, must be a list of strings (or a single string for post_format).
    post = raw.get("post") or {}
    if not isinstance(post, dict):
        raise ValueError(f"{path}: 'post' must be a mapping")
    def _str_tuple(key: str) -> tuple[str, ...]:
        val = post.get(key) or []
        if not isinstance(val, list):
            raise ValueError(f"{path}: post.{key} must be a list of strings")
        out = tuple(str(x) for x in val)
        # Validate regexes at parse time so bad patterns fail loudly, not silently
        import re as _re
        for pat in out:
            try:
                _re.compile(pat)
            except _re.error as e:
                raise ValueError(f"{path}: post.{key} contains invalid regex {pat!r}: {e}")
        return out

    post_strip_prefixes = _str_tuple("strip_prefixes")
    post_skip_if_matches = _str_tuple("skip_if_matches")
    post_format = post.get("format")
    if post_format is not None and not isinstance(post_format, str):
        raise ValueError(f"{path}: post.format must be a string or absent")
    if post_format is not None and "{text}" not in post_format:
        raise ValueError(f"{path}: post.format must contain '{{text}}' placeholder, got {post_format!r}")

    backend = str(raw.get("backend", "openai")).strip().lower()
    if backend not in ("openai", "anthropic"):
        raise ValueError(f"{path}: backend must be 'openai' or 'anthropic', got {backend!r}")
    cache_system_raw = raw.get("cache_system")
    cache_system = True if cache_system_raw is None else bool(cache_system_raw)
    anthropic_version = str(raw.get("anthropic_version", "2023-06-01"))

    return Profile(
        name=name,
        url=str(raw["url"]),
        model=str(raw["model"]),
        system=str(raw["system"]),
        labels=tuple(str(x) for x in labels),
        fallback=str(fallback),
        temperature=float(raw.get("temperature", 0.0)),
        timeout_s=float(raw.get("timeout_s", 10.0)),
        api_key_service=str(raw["api_key_service"]) if raw.get("api_key_service") else None,
        backend=backend,
        cache_system=cache_system,
        anthropic_version=anthropic_version,
        post_strip_prefixes=post_strip_prefixes,
        post_skip_if_matches=post_skip_if_matches,
        post_format=post_format,
    )


def load_profile(name: str) -> Optional[Profile]:
    """Load a profile by name from the configured search dirs.

    Returns None (with a warning log on first miss) if no matching YAML
    exists — caller should treat this as "classifier unavailable." Raises
    ValueError if the file exists but is malformed; that's a deploy error
    worth surfacing loudly.
    """
    with _PROFILE_CACHE_LOCK:
        hit = _PROFILE_CACHE.get(name)
        if hit is not None:
            return hit
    path = _find_profile_file(name)
    if path is None:
        logger.warning(
            f"SLM profile {name!r} not found in search dirs; returning None. "
            f"Searched: {[str(d) for d in _profile_search_dirs()]}"
        )
        return None
    prof = _parse_profile(name, path)
    with _PROFILE_CACHE_LOCK:
        _PROFILE_CACHE[name] = prof
    return prof


# ── Classification ────────────────────────────────────────────────────────────
def _resolve_api_key(service: Optional[str]) -> Optional[str]:
    if not service:
        return None
    # Lazy import to avoid the auth_utils → m3_sdk cycle at module load.
    try:
        from auth_utils import get_api_key
        return get_api_key(service)
    except Exception as e:
        logger.debug(f"api_key lookup for {service!r} failed: {e}")
        return None


async def _call_model(
    prof: Profile,
    user_text: str,
    client: httpx.AsyncClient,
) -> str:
    """Send a single chat-completion request per the profile's backend and
    return the raw assistant reply string.

    Dispatches on prof.backend:
      - "openai" (default): POST {url} with OpenAI chat/completions body.
        Expects `choices[0].message.content` in the response.
      - "anthropic": POST {url} with Anthropic /v1/messages body. System
        prompt becomes a top-level `system` field (optionally wrapped in
        a cache_control block when prof.cache_system). Expects
        `content[0].text` in the response.

    Raises httpx.HTTPError / KeyError / json.JSONDecodeError / TimeoutError
    to the caller, which logs + returns None/[] per its contract.
    """
    token = _resolve_api_key(prof.api_key_service)
    headers = {"Content-Type": "application/json"}

    if prof.backend == "anthropic":
        # Anthropic wants x-api-key, not Authorization: Bearer.
        if token:
            headers["x-api-key"] = token
        headers["anthropic-version"] = prof.anthropic_version

        # System prompt: either a plain string or a list-of-blocks wrapping
        # the text in a cache_control ephemeral block.
        if prof.cache_system and prof.system:
            system_field = [{
                "type": "text",
                "text": prof.system,
                "cache_control": {"type": "ephemeral"},
            }]
        else:
            system_field = prof.system

        payload = {
            "model": prof.model,
            "max_tokens": 512,
            "system": system_field,
            "messages": [{"role": "user", "content": user_text}],
            "temperature": prof.temperature,
        }
        resp = await client.post(prof.url, headers=headers, json=payload, timeout=prof.timeout_s)
        resp.raise_for_status()
        data = resp.json()
        # Anthropic: content is a list of content blocks. Concat all text blocks.
        blocks = data.get("content", [])
        parts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        return "".join(parts)

    # Default / "openai" backend.
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {
        "model": prof.model,
        "messages": [
            {"role": "system", "content": prof.system},
            {"role": "user", "content": user_text},
        ],
        "temperature": prof.temperature,
    }
    resp = await client.post(prof.url, headers=headers, json=payload, timeout=prof.timeout_s)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def _apply_post(raw: str, profile: Profile) -> str:
    """Apply profile-declared post-processing to a raw SLM reply.

    Order:
      1. skip_if_matches — if ANY pattern matches (re.search, case-insensitive,
         multiline), return "" to signal "no extractable content."
      2. strip_prefixes — strip matching prefixes from the start of the
         output, applied in the order declared. Case-insensitive.
      3. format — wrap the cleaned text in a template containing {text}.

    An empty/None raw input returns "" regardless of rules.
    """
    import re as _re
    if not raw:
        return ""
    text = raw.strip()
    # 1. skip-if-matches
    for pat in profile.post_skip_if_matches:
        if _re.search(pat, text, _re.IGNORECASE | _re.MULTILINE):
            return ""
    # 2. strip-prefixes (repeat until no prefix matches, so stacked prefixes
    #    like "Sure. Here are the facts:" both get stripped)
    changed = True
    while changed:
        changed = False
        for pat in profile.post_strip_prefixes:
            m = _re.match(pat, text, _re.IGNORECASE)
            if m:
                text = text[m.end():].lstrip()
                changed = True
                break
    # 3. format wrap
    if profile.post_format:
        text = profile.post_format.format(text=text)
    return text


def _pick_label(raw_output: str, profile: Profile) -> str:
    """Match the model's raw reply against the profile's label list.

    Exact match first (case-insensitive); then substring. Falls through to
    the profile-declared fallback. The fallback is deliberately never
    None — callers can always rely on getting a valid label string.
    """
    text = (raw_output or "").strip().lower()
    if not text:
        return profile.fallback
    labels_lower = [lbl.lower() for lbl in profile.labels]
    if text in labels_lower:
        return profile.labels[labels_lower.index(text)]
    for i, lbl in enumerate(labels_lower):
        if lbl in text:
            return profile.labels[i]
    return profile.fallback


async def extract_entities(
    text: str,
    profile: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[list[str]]:
    """Free-text entity extraction via a named profile.

    Sibling of ``classify_intent`` that reuses the profile-loader machinery
    but returns a list of entities parsed from the model's reply instead of
    a single label. The profile's ``labels`` field is ignored; whatever the
    model returns is split by commas/newlines, trimmed, and filtered to
    items <= 60 chars.

    Returns ``None`` (not ``[]``) when the SLM gate is off, the profile is
    missing, or the HTTP call fails — callers distinguish "no signal" from
    "signal, but empty list."
    """
    if not _gate_on():
        return None
    if not text or not text.strip():
        return None

    prof_name = (profile or "").strip() or "entity_extract"
    prof = load_profile(prof_name)
    if prof is None:
        return None

    owns_client = client is None
    try:
        if owns_client:
            client = httpx.AsyncClient(timeout=prof.timeout_s)
        raw = await _call_model(prof, text, client)
    except (httpx.HTTPError, KeyError, json.JSONDecodeError, TimeoutError) as e:
        logger.warning(f"SLM extract via profile={prof_name!r} failed: {type(e).__name__}: {e}")
        return None
    finally:
        if owns_client and client is not None:
            await client.aclose()

    # Apply profile post-processing BEFORE splitting so preamble-strip and
    # skip_if_matches fire on the whole reply, not on individual pieces.
    cleaned = _apply_post(raw or "", prof)
    # Split on commas and newlines, strip quotes/whitespace, drop empties
    # and pathologically long items.
    entities: list[str] = []
    for piece in cleaned.replace("\n", ",").split(","):
        clean = piece.strip().strip('"').strip("'")
        if clean and len(clean) <= 60:
            entities.append(clean)
    return entities


async def extract_text(
    text: str,
    profile: str,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[str]:
    """Free-text SLM extraction via a named profile, returning the raw
    model output unchanged.

    Sibling of ``extract_entities`` for callers that want the model's
    reply verbatim — e.g. contextual-keys ingest enrichment where the
    output is a pipe-separated fact list meant to be prepended to
    embed_text as-is. No splitting, no filtering, no length caps.

    Returns ``None`` (not ``""``) when the SLM gate is off, the profile
    is missing, the input is empty, or the HTTP call fails. Empty-string
    replies are returned as empty strings so callers can distinguish
    "no signal" from "signal, no extractable content."

    ``profile`` is required — unlike classify_intent, there is no
    sensible default for free-text extraction.
    """
    if not _gate_on():
        return None
    if not text or not text.strip():
        return None
    if not profile or not profile.strip():
        return None

    prof = load_profile(profile.strip())
    if prof is None:
        return None

    owns_client = client is None
    try:
        if owns_client:
            client = httpx.AsyncClient(timeout=prof.timeout_s)
        raw = await _call_model(prof, text, client)
    except (httpx.HTTPError, KeyError, json.JSONDecodeError, TimeoutError) as e:
        logger.warning(f"SLM extract_text via profile={profile!r} failed: {type(e).__name__}: {e}")
        return None
    finally:
        if owns_client and client is not None:
            await client.aclose()

    # Apply profile post-processing (strip preambles, skip refusals, format
    # wrap). Idempotent no-op when the profile declares no post rules.
    return _apply_post(raw or "", prof)


async def classify_intent(
    query: str,
    profile: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> Optional[str]:
    """Classify ``query`` using the SLM configured by ``profile``.

    Returns a label string from the profile's ``labels`` list, or the
    profile's ``fallback`` if the model's reply doesn't match any label.
    Returns ``None`` when:
      - ``M3_SLM_CLASSIFIER`` gate is off (the common default path)
      - the named profile can't be found or is malformed
      - the HTTP call fails (logged at WARNING)

    Callers should treat ``None`` as "no intent signal; proceed with
    whatever heuristic you had before." This keeps the gate strictly
    additive — turning it off never changes behavior, only information
    available to the caller.

    ``client`` lets callers inject a shared ``httpx.AsyncClient`` for
    connection pooling across many classifications (e.g. a bench run
    scoring 500 questions).
    """
    if not _gate_on():
        return None
    if not query or not query.strip():
        return None

    prof_name = (profile or "").strip() or _default_profile_name()
    prof = load_profile(prof_name)
    if prof is None:
        return None

    owns_client = client is None
    try:
        if owns_client:
            client = httpx.AsyncClient(timeout=prof.timeout_s)
        raw = await _call_model(prof, query, client)
    except (httpx.HTTPError, KeyError, json.JSONDecodeError, TimeoutError) as e:
        logger.warning(f"SLM classify via profile={prof_name!r} failed: {type(e).__name__}: {e}")
        return None
    finally:
        if owns_client and client is not None:
            await client.aclose()

    return _pick_label(raw, prof)


# ── Diagnostics ───────────────────────────────────────────────────────────────
def list_profiles() -> dict[str, Optional[Path]]:
    """Return a {name: path-or-None} map of profiles discoverable on this host.

    Useful for operators — ``python -m slm_intent`` prints this. Resolves
    every .yaml in every search dir; collisions resolve to the first-dir
    winner (same rule as load_profile).
    """
    found: dict[str, Path] = {}
    for d in _profile_search_dirs():
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.yaml")):
            name = p.stem
            if name not in found:
                found[name] = p
    return found


def _selftest() -> None:
    gate = "on" if _gate_on() else "off"
    print(f"M3_SLM_CLASSIFIER: {gate}")
    print(f"M3_SLM_PROFILE:    {_default_profile_name()}")
    print(f"search dirs:")
    for d in _profile_search_dirs():
        exists = "[OK]  " if d.is_dir() else "[none]"
        print(f"  {exists} {d}")
    print(f"profiles found:")
    profs = list_profiles()
    if not profs:
        print("  (none)")
    else:
        for name, path in profs.items():
            print(f"  {name:20s} {path}")


if __name__ == "__main__":
    _selftest()
