"""Tests for runtime tier-1 GGUF auto-detection (memory.embed.discover_bge_m3_gguf).

When M3_EMBED_GGUF is unset, tier-1 (the ~10-85x faster in-process embedder)
should activate automatically by finding a bge-m3 GGUF in the canonical model
dirs — bounded by depth and a wall-clock budget so a huge models directory can't
stall cold start. Opt out with M3_EMBED_GGUF_AUTODETECT=0.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from memory import embed as E  # noqa: E402


def _make_gguf(root: Path, rel: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"GGUF\x00fake")
    return p


def test_finds_bge_m3_in_lmstudio_layout(monkeypatch, tmp_path):
    """A bge-m3 GGUF in the LM Studio org/model/file layout is found."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    target = _make_gguf(tmp_path, ".lmstudio/models/deepsweet/bge-m3-GGUF-Q4_K_M/bge-m3-GGUF-Q4_K_M.gguf")
    found = E.discover_bge_m3_gguf()
    assert found is not None
    assert Path(found) == target


def test_ignores_non_bge_m3_gguf(monkeypatch, tmp_path):
    """A different model's GGUF must NOT be picked — only bge-m3 (correct dim/model)."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _make_gguf(tmp_path, ".lmstudio/models/other/llama-3-8b/llama-3-8b.gguf")
    assert E.discover_bge_m3_gguf() is None


def test_matches_underscore_variant(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    target = _make_gguf(tmp_path, "models/bge_m3-q4.gguf")
    assert Path(E.discover_bge_m3_gguf()) == target


def test_returns_none_when_no_model_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert E.discover_bge_m3_gguf() is None


def test_respects_depth_bound(monkeypatch, tmp_path):
    """A GGUF buried far deeper than the LM Studio layout (depth > 4) is skipped."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _make_gguf(tmp_path, ".lmstudio/models/a/b/c/d/e/f/bge-m3.gguf")  # depth 7
    assert E.discover_bge_m3_gguf() is None


def test_budget_bounds_walltime(monkeypatch, tmp_path):
    """A tiny budget returns quickly even if a match exists deep in a big tree."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    # Many sibling files so the walk has work to do.
    for i in range(50):
        _make_gguf(tmp_path, f".lmstudio/models/org{i}/model/file{i}.gguf")
    t0 = time.monotonic()
    E.discover_bge_m3_gguf(budget_s=0.001)  # near-zero budget
    assert time.monotonic() - t0 < 1.0  # returns fast, never hangs


@pytest.mark.asyncio
async def test_autodetect_wires_into_embedder_resolution(monkeypatch, tmp_path):
    """With env unset + autodetect on, _get_embedded_embedder picks up the
    detected path (we stub the native extension so no real model loads)."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    target = _make_gguf(tmp_path, ".lmstudio/models/deepsweet/bge-m3/bge-m3.gguf")

    # Reset the module's resolution state + env.
    monkeypatch.setattr(E, "_EMBED_GGUF_PATH", None, raising=False)
    monkeypatch.setattr(E, "_embedded_embedder", None, raising=False)
    monkeypatch.setattr(E, "_embedded_embed_checked", False, raising=False)
    monkeypatch.setattr(E, "_EMBED_GGUF_AUTODETECT", True, raising=False)

    # Stub the native extension: a fake EmbeddedEmbedder that records the path.
    seen = {}

    class _FakeEmb:
        def __init__(self, path):
            seen["path"] = path

        def embedding_dim(self):
            return E.config.EMBED_DIM  # pass the dim guard

    monkeypatch.setattr(E.config, "m3_core_rs",
                        type("RS", (), {"EmbeddedEmbedder": _FakeEmb}))

    emb = E._get_embedded_embedder()
    assert emb is not None
    assert Path(seen["path"]) == target


def test_autodetect_off_skips_discovery(monkeypatch, tmp_path):
    """M3_EMBED_GGUF_AUTODETECT=0 must skip discovery entirely."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    _make_gguf(tmp_path, ".lmstudio/models/x/bge-m3/bge-m3.gguf")
    monkeypatch.setattr(E, "_EMBED_GGUF_PATH", None, raising=False)
    monkeypatch.setattr(E, "_embedded_embedder", None, raising=False)
    monkeypatch.setattr(E, "_embedded_embed_checked", False, raising=False)
    monkeypatch.setattr(E, "_EMBED_GGUF_AUTODETECT", False, raising=False)
    # Native extension present, but autodetect off -> no path -> None.
    monkeypatch.setattr(E.config, "m3_core_rs",
                        type("RS", (), {"EmbeddedEmbedder": object}))
    assert E._get_embedded_embedder() is None
