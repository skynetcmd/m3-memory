"""Tests for bin/slm_intent.py — profile loader + gate + label matching.

We deliberately don't hit a real SLM endpoint here; classify_intent and
extract_entities are async and take an injectable httpx client, but the
tests instead exercise the gate, the profile-loading machinery, and the
label-matching logic that runs *after* the HTTP call. Network behavior
is covered by the bench harness in an end-to-end run.
"""
from __future__ import annotations

import asyncio

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
