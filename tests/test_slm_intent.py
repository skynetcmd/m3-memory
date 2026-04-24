"""Tests for bin/slm_intent.py — profile loader + gate + label matching.

We deliberately don't hit a real SLM endpoint here; classify_intent and
extract_entities are async and take an injectable httpx client, but the
tests instead exercise the gate, the profile-loading machinery, and the
label-matching logic that runs *after* the HTTP call. Network behavior
is covered by the bench harness in an end-to-end run.
"""
from __future__ import annotations

import asyncio
import json

import pytest


@pytest.fixture(autouse=True)
def _isolate_slm_env(monkeypatch):
    """Strip SLM env vars between tests so no test leaks state."""
    for k in ("M3_SLM_CLASSIFIER", "M3_SLM_PROFILE", "M3_SLM_PROFILES_DIR"):
        monkeypatch.delenv(k, raising=False)
    import slm_intent
    slm_intent.invalidate_cache()
    yield
    slm_intent.invalidate_cache()


def _write_profile(path, **fields):
    """Build a minimal valid profile YAML with fields overridable."""
    import yaml
    base = {
        "url": "http://127.0.0.1:0/",  # unroutable, never actually called
        "model": "test-model",
        "system": "test system prompt",
        "labels": ["a", "b", "c"],
        "fallback": "c",
        "temperature": 0,
        "timeout_s": 1.0,
    }
    base.update(fields)
    path.write_text(yaml.safe_dump(base), encoding="utf-8")


def test_classify_intent_returns_none_when_gate_off(tmp_path, monkeypatch):
    """Gate off → immediate None, no profile load attempted."""
    import slm_intent

    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    # Intentionally don't set M3_SLM_CLASSIFIER.
    result = asyncio.run(slm_intent.classify_intent("anything", profile="default"))
    assert result is None


def test_classify_intent_returns_none_for_empty_query(tmp_path, monkeypatch):
    """Empty / whitespace-only input returns None without calling the SLM."""
    import slm_intent

    _write_profile(tmp_path / "default.yaml")
    monkeypatch.setenv("M3_SLM_CLASSIFIER", "1")
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()

    assert asyncio.run(slm_intent.classify_intent("")) is None
    assert asyncio.run(slm_intent.classify_intent("   ")) is None


def test_load_profile_missing_returns_none(tmp_path, monkeypatch):
    """Requesting a profile that doesn't exist logs + returns None."""
    import slm_intent

    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()
    assert slm_intent.load_profile("nonexistent") is None


def test_load_profile_cached_across_calls(tmp_path, monkeypatch):
    """Second call returns the cached object (same identity)."""
    import slm_intent

    _write_profile(tmp_path / "cached.yaml")
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()

    p1 = slm_intent.load_profile("cached")
    p2 = slm_intent.load_profile("cached")
    assert p1 is p2


def test_load_profile_malformed_raises(tmp_path, monkeypatch):
    """Missing required keys surface as ValueError, not a silent fallback."""
    import slm_intent

    (tmp_path / "broken.yaml").write_text("url: http://x\n", encoding="utf-8")
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()

    with pytest.raises(ValueError, match="missing required keys"):
        slm_intent.load_profile("broken")


def test_load_profile_rejects_fallback_not_in_labels(tmp_path, monkeypatch):
    """fallback must be one of the declared labels."""
    import slm_intent

    _write_profile(tmp_path / "bad_fallback.yaml", fallback="not-a-label")
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()

    with pytest.raises(ValueError, match="fallback"):
        slm_intent.load_profile("bad_fallback")


def test_list_profiles_discovers_yaml_files(tmp_path, monkeypatch):
    """list_profiles() returns a name→path map for every .yaml in search dirs."""
    import slm_intent

    _write_profile(tmp_path / "one.yaml")
    _write_profile(tmp_path / "two.yaml")
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()

    profs = slm_intent.list_profiles()
    assert "one" in profs
    assert "two" in profs


def test_profile_search_dir_stacking(tmp_path, monkeypatch):
    """Multiple dirs in M3_SLM_PROFILES_DIR resolve first-match-wins."""
    import os
    import slm_intent

    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    # Same name in both dirs; first dir's should win.
    _write_profile(dir_a / "shared.yaml", model="from-a")
    _write_profile(dir_b / "shared.yaml", model="from-b")
    monkeypatch.setenv(
        "M3_SLM_PROFILES_DIR",
        f"{dir_a}{os.pathsep}{dir_b}",
    )
    slm_intent.invalidate_cache()

    prof = slm_intent.load_profile("shared")
    assert prof.model == "from-a"


def test_pick_label_exact_match(tmp_path, monkeypatch):
    """_pick_label returns exact-match label with case folding."""
    import slm_intent

    _write_profile(tmp_path / "p.yaml", labels=["user-fact", "temporal", "general"], fallback="general")
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()

    prof = slm_intent.load_profile("p")
    assert slm_intent._pick_label("user-fact", prof) == "user-fact"
    assert slm_intent._pick_label("USER-FACT", prof) == "user-fact"
    assert slm_intent._pick_label("  temporal  ", prof) == "temporal"


def test_pick_label_substring_match(tmp_path, monkeypatch):
    """Falls back to substring when model returns prose not matching exactly."""
    import slm_intent

    _write_profile(tmp_path / "p.yaml", labels=["user-fact", "general"], fallback="general")
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()

    prof = slm_intent.load_profile("p")
    # Model says "This is a user-fact question" → extract user-fact
    assert slm_intent._pick_label("This is a user-fact question", prof) == "user-fact"


def test_pick_label_fallback_on_mismatch(tmp_path, monkeypatch):
    """When the model's reply matches no label, fallback wins."""
    import slm_intent

    _write_profile(tmp_path / "p.yaml", labels=["a", "b"], fallback="b")
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()

    prof = slm_intent.load_profile("p")
    assert slm_intent._pick_label("zzz", prof) == "b"
    assert slm_intent._pick_label("", prof) == "b"


# ── extract_text tests ───────────────────────────────────────────────────────

def test_extract_text_returns_none_when_gate_off(tmp_path, monkeypatch):
    """Gate off → immediate None, no profile load attempted."""
    import slm_intent

    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    # Intentionally don't set M3_SLM_CLASSIFIER.
    result = asyncio.run(slm_intent.extract_text("anything", profile="default"))
    assert result is None


def test_extract_text_returns_none_for_empty_input(tmp_path, monkeypatch):
    """Empty / whitespace-only input returns None without calling the SLM."""
    import slm_intent

    _write_profile(tmp_path / "default.yaml")
    monkeypatch.setenv("M3_SLM_CLASSIFIER", "1")
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()

    assert asyncio.run(slm_intent.extract_text("", profile="default")) is None
    assert asyncio.run(slm_intent.extract_text("   ", profile="default")) is None


def test_extract_text_requires_profile(tmp_path, monkeypatch):
    """No fallback default for extract_text — empty profile returns None."""
    import slm_intent

    _write_profile(tmp_path / "default.yaml")
    monkeypatch.setenv("M3_SLM_CLASSIFIER", "1")
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()

    assert asyncio.run(slm_intent.extract_text("hello", profile="")) is None
    assert asyncio.run(slm_intent.extract_text("hello", profile="   ")) is None


def test_extract_text_returns_none_when_profile_missing(tmp_path, monkeypatch):
    """Profile not found → None."""
    import slm_intent

    monkeypatch.setenv("M3_SLM_CLASSIFIER", "1")
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()

    assert asyncio.run(slm_intent.extract_text("hello", profile="nonexistent")) is None


# ── post-processing tests ────────────────────────────────────────────────────

def _write_profile_with_post(path, post_block):
    """Write a minimal profile with a `post:` block."""
    import yaml
    base = {
        "url": "http://x/v1/chat/completions",
        "model": "m",
        "system": "s",
        "labels": ["a", "b"],
        "fallback": "a",
        "post": post_block,
    }
    path.write_text(yaml.safe_dump(base), encoding="utf-8")


def test_apply_post_strip_prefixes_single(tmp_path, monkeypatch):
    """strip_prefixes removes the matched prefix from the start."""
    import slm_intent

    _write_profile_with_post(
        tmp_path / "p.yaml",
        {"strip_prefixes": [r"^here are (the )?facts?\s*:?\s*"]},
    )
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()
    prof = slm_intent.load_profile("p")

    assert slm_intent._apply_post("Here are facts: A | B | C", prof) == "A | B | C"
    assert slm_intent._apply_post("here are the facts: X | Y", prof) == "X | Y"
    # Non-matching prefix passes through unchanged
    assert slm_intent._apply_post("Just A | B", prof) == "Just A | B"


def test_apply_post_strip_prefixes_stacked(tmp_path, monkeypatch):
    """Multiple prefixes peel iteratively until none match."""
    import slm_intent

    _write_profile_with_post(
        tmp_path / "p.yaml",
        {"strip_prefixes": [r"^sure[,.]?\s*", r"^here are (the )?facts?\s*:?\s*"]},
    )
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()
    prof = slm_intent.load_profile("p")

    # "Sure. " then "Here are facts: " both stripped
    assert slm_intent._apply_post("Sure. Here are facts: X | Y", prof) == "X | Y"


def test_apply_post_skip_if_matches(tmp_path, monkeypatch):
    """skip_if_matches returns '' when ANY pattern matches."""
    import slm_intent

    _write_profile_with_post(
        tmp_path / "p.yaml",
        {"skip_if_matches": [r"^[-_.\s]*$", r"no (extractable )?facts"]},
    )
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()
    prof = slm_intent.load_profile("p")

    assert slm_intent._apply_post("-", prof) == ""
    assert slm_intent._apply_post("...", prof) == ""
    assert slm_intent._apply_post("no facts", prof) == ""
    assert slm_intent._apply_post("No extractable facts found", prof) == ""
    # Normal content passes through
    assert slm_intent._apply_post("A | B | C", prof) == "A | B | C"


def test_apply_post_format_wrapper(tmp_path, monkeypatch):
    """format wraps cleaned text in a template containing {text}."""
    import slm_intent

    _write_profile_with_post(
        tmp_path / "p.yaml",
        {"format": "[FACTS] {text}"},
    )
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()
    prof = slm_intent.load_profile("p")

    assert slm_intent._apply_post("A | B", prof) == "[FACTS] A | B"
    # Empty input stays empty (wrapper doesn't force signal on nothing)
    assert slm_intent._apply_post("", prof) == ""


def test_apply_post_combined_pipeline(tmp_path, monkeypatch):
    """Full pipeline: skip, then strip, then format."""
    import slm_intent

    _write_profile_with_post(
        tmp_path / "p.yaml",
        {
            "skip_if_matches": [r"^-$"],
            "strip_prefixes": [r"^facts?\s*:\s*"],
            "format": "<<{text}>>",
        },
    )
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()
    prof = slm_intent.load_profile("p")

    assert slm_intent._apply_post("-", prof) == ""
    assert slm_intent._apply_post("facts: X", prof) == "<<X>>"
    assert slm_intent._apply_post("Y | Z", prof) == "<<Y | Z>>"


def test_apply_post_default_noop(tmp_path, monkeypatch):
    """Profiles without `post:` leave output unchanged (post is all optional)."""
    import slm_intent

    _write_profile(tmp_path / "default.yaml")  # no post block
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()
    prof = slm_intent.load_profile("default")

    assert slm_intent._apply_post("anything", prof) == "anything"
    assert slm_intent._apply_post("  trims  ", prof) == "trims"
    assert slm_intent._apply_post("", prof) == ""


def test_parse_profile_rejects_invalid_regex_in_post(tmp_path, monkeypatch):
    """Bad regex in post.strip_prefixes fails loudly at load time.

    load_profile raises ValueError for malformed profiles (not None) — a
    deploy-error-worth-surfacing situation per the docstring contract.
    """
    import slm_intent

    _write_profile_with_post(
        tmp_path / "bad.yaml",
        {"strip_prefixes": ["[invalid("]},
    )
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()

    with pytest.raises(ValueError, match="invalid regex"):
        slm_intent.load_profile("bad")


def test_parse_profile_rejects_format_without_placeholder(tmp_path, monkeypatch):
    """post.format without {text} is rejected with a clear ValueError."""
    import slm_intent

    _write_profile_with_post(
        tmp_path / "bad.yaml",
        {"format": "just a string"},
    )
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()

    with pytest.raises(ValueError, match=r"post\.format must contain"):
        slm_intent.load_profile("bad")


# ── Backend dispatch tests ───────────────────────────────────────────────────

def test_parse_profile_default_backend_is_openai(tmp_path, monkeypatch):
    """Profiles without `backend:` default to 'openai' (back-compat)."""
    import slm_intent

    _write_profile(tmp_path / "default.yaml")
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()

    prof = slm_intent.load_profile("default")
    assert prof.backend == "openai"
    assert prof.cache_system is True
    assert prof.anthropic_version == "2023-06-01"


def test_parse_profile_backend_anthropic(tmp_path, monkeypatch):
    """Profile can declare backend: anthropic."""
    import slm_intent

    _write_profile(tmp_path / "p.yaml", backend="anthropic", cache_system=False)
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()

    prof = slm_intent.load_profile("p")
    assert prof.backend == "anthropic"
    assert prof.cache_system is False


def test_parse_profile_rejects_unknown_backend(tmp_path, monkeypatch):
    """Unknown backend is a deploy error."""
    import slm_intent

    _write_profile(tmp_path / "bad.yaml", backend="gemini")
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()

    with pytest.raises(ValueError, match="backend must be"):
        slm_intent.load_profile("bad")


@pytest.mark.asyncio
async def test_call_model_openai_body_shape(tmp_path, monkeypatch):
    """_call_model with backend=openai sends OpenAI chat/completions body."""
    import slm_intent
    import httpx

    _write_profile(tmp_path / "default.yaml", api_key_service=None)
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()
    prof = slm_intent.load_profile("default")
    assert prof.backend == "openai"

    captured_request = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_request["url"] = str(request.url)
        captured_request["json"] = json.loads(request.content)
        captured_request["headers"] = dict(request.headers)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "openai-reply"}}]},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    try:
        result = await slm_intent._call_model(prof, "hello", client)
    finally:
        await client.aclose()

    assert result == "openai-reply"
    # OpenAI body shape: messages list includes system as a role
    assert captured_request["json"]["messages"][0]["role"] == "system"
    assert captured_request["json"]["messages"][1]["role"] == "user"
    assert captured_request["json"]["messages"][1]["content"] == "hello"
    # OpenAI uses Authorization: Bearer when api key present; none set here
    assert "anthropic-version" not in {k.lower() for k in captured_request["headers"]}


@pytest.mark.asyncio
async def test_call_model_anthropic_body_shape(tmp_path, monkeypatch):
    """_call_model with backend=anthropic sends Anthropic messages body,
    with system as a top-level field wrapped in cache_control."""
    import slm_intent
    import httpx

    _write_profile(tmp_path / "a.yaml", backend="anthropic", api_key_service=None)
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()
    prof = slm_intent.load_profile("a")
    assert prof.backend == "anthropic"
    assert prof.cache_system is True  # default

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "anthropic-reply"}]},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    try:
        result = await slm_intent._call_model(prof, "hello", client)
    finally:
        await client.aclose()

    assert result == "anthropic-reply"
    body = captured["json"]
    # Anthropic shape: system top-level, messages only user
    assert "messages" in body
    assert body["messages"] == [{"role": "user", "content": "hello"}]
    # System is a cache_control-wrapped block list when cache_system=True
    assert isinstance(body["system"], list)
    assert body["system"][0]["type"] == "text"
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    # Anthropic version header present
    assert captured["headers"]["anthropic-version"] == "2023-06-01"


@pytest.mark.asyncio
async def test_call_model_anthropic_no_cache(tmp_path, monkeypatch):
    """When cache_system=False, system is sent as a plain string."""
    import slm_intent
    import httpx

    _write_profile(
        tmp_path / "a.yaml",
        backend="anthropic", cache_system=False, api_key_service=None,
    )
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()
    prof = slm_intent.load_profile("a")

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "ok"}]},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    try:
        await slm_intent._call_model(prof, "hello", client)
    finally:
        await client.aclose()

    # Plain string, not wrapped in a block list
    assert isinstance(captured["json"]["system"], str)
    assert captured["json"]["system"] == "test system prompt"


@pytest.mark.asyncio
async def test_call_model_anthropic_uses_x_api_key(tmp_path, monkeypatch):
    """When an API key resolves, anthropic backend uses x-api-key header."""
    import slm_intent
    import httpx

    _write_profile(tmp_path / "a.yaml", backend="anthropic", api_key_service="FAKE_KEY_SVC")
    monkeypatch.setenv("M3_SLM_PROFILES_DIR", str(tmp_path))
    slm_intent.invalidate_cache()

    # Monkey-patch _resolve_api_key to return a known string without
    # hitting the keyring.
    monkeypatch.setattr(slm_intent, "_resolve_api_key", lambda s: "fake-key-12345")

    prof = slm_intent.load_profile("a")

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return httpx.Response(
            200, json={"content": [{"type": "text", "text": "ok"}]},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    try:
        await slm_intent._call_model(prof, "hello", client)
    finally:
        await client.aclose()

    # Anthropic uses x-api-key, NOT Authorization Bearer
    assert captured["headers"]["x-api-key"] == "fake-key-12345"
    assert "authorization" not in captured["headers"]
