"""M3_* env-var namespacing migration: the new name wins, the old still works.

Config constants are read at import time, so each case reloads memory.config
with a fresh environment. Covers a representative sample of the migrated vars
(int/float/str) to prove the getenv_compat wiring, not just the helper.
"""
from __future__ import annotations

import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))


def _reload_config(monkeypatch, env: dict):
    # Clear both names for every var we touch, then set only what the case wants.
    for var in ("M3_DEDUP_THRESHOLD", "DEDUP_THRESHOLD",
                "M3_EMBED_DIM", "EMBED_DIM",
                "M3_CONTRADICTION_TITLE_GATE", "CONTRADICTION_TITLE_GATE"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import memory.config as c
    importlib.reload(c)
    return c


def test_m3_prefixed_name_is_honored(monkeypatch):
    c = _reload_config(monkeypatch, {"M3_DEDUP_THRESHOLD": "0.5", "M3_EMBED_DIM": "384"})
    assert c.DEDUP_THRESHOLD == 0.5
    assert c.EMBED_DIM == 384


def test_bare_legacy_name_still_resolves(monkeypatch):
    c = _reload_config(monkeypatch, {"DEDUP_THRESHOLD": "0.7",
                                     "CONTRADICTION_TITLE_GATE": "strict"})
    assert c.DEDUP_THRESHOLD == 0.7
    assert c.CONTRADICTION_TITLE_GATE == "strict"


def test_m3_prefixed_beats_legacy(monkeypatch):
    c = _reload_config(monkeypatch, {"M3_DEDUP_THRESHOLD": "0.5",
                                     "DEDUP_THRESHOLD": "0.9"})
    assert c.DEDUP_THRESHOLD == 0.5  # new name wins


def test_default_applies_when_neither_set(monkeypatch):
    c = _reload_config(monkeypatch, {})
    assert c.DEDUP_THRESHOLD == 0.92  # documented default
    assert c.EMBED_DIM == 1024


def test_legacy_read_is_recorded_for_doctor(monkeypatch):
    # getenv_compat records the deprecated name so `m3 doctor` can surface it.
    from m3_core.paths import deprecated_env_in_use
    _reload_config(monkeypatch, {"DEDUP_THRESHOLD": "0.7"})
    seen = deprecated_env_in_use()
    assert seen.get("DEDUP_THRESHOLD") == "M3_DEDUP_THRESHOLD"
