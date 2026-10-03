"""`memory.embed` must import even when `config.m3_core_rs` is not a real core.

Four circuit breakers are constructed at MODULE level, each reaching into
`config.m3_core_rs.CircuitBreaker`. An exception there makes `memory.embed`
un-importable, and because `memory/__init__` imports it the failure surfaces far
from its cause: on 2026-10-02 it appeared as five failures in
`test_oxidation_probe` reporting "could not import memory.config:
AttributeError: 'object' object has no attribute 'CircuitBreaker'".

The conditions were ordinary: one test stubs the core with `object()` to
simulate the disabled state, another pops `memory.embed` from `sys.modules` and
re-imports it. Neither is wrong on its own.

Returning a None breaker is the fallback this module already documents, so the
only question these tests pin is that the import SURVIVES.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


def _reimport_embed():
    sys.modules.pop("memory.embed", None)
    return importlib.import_module("memory.embed")


@pytest.fixture
def restore_embed():
    """Put back the module object that was cached before this test.

    A pop-then-reimport also leaves a valid module cached, but a different object
    with freshly rebuilt state. Restoring the original keeps one instance, which
    is the property that matters to whatever runs next.
    """
    saved = sys.modules.get("memory.embed")
    yield
    if saved is None:
        sys.modules.pop("memory.embed", None)
        importlib.import_module("memory.embed")
        return
    sys.modules["memory.embed"] = saved
    import memory
    memory.embed = saved


@pytest.mark.parametrize(
    "stub",
    [
        pytest.param(object(), id="bare-object"),
        pytest.param(type("Partial", (), {})(), id="instance-without-the-attr"),
        pytest.param(type("PartialType", (), {}), id="class-without-the-attr"),
    ],
)
def test_import_survives_a_core_that_lacks_circuitbreaker(monkeypatch, restore_embed, stub):
    from memory import config

    monkeypatch.setattr(config, "m3_core_rs", stub, raising=False)
    mod = _reimport_embed()
    assert mod._EMBEDDED_BREAKER is None
    assert mod._CPU_FALLBACK_BREAKER is None


def test_a_present_but_broken_core_WARNS_rather_than_degrading_silently(
    monkeypatch, restore_embed, caplog
):
    """Fail-safe is not licence to be silent (DESIGN_PHILOSOPHIES §3).

    `rs is None` is the documented fallback and stays quiet. A core that is
    PRESENT and lacks CircuitBreaker is a broken install: breakers end up
    disabled, which changes retry behaviour, so it must be visible.
    """
    from memory import config

    monkeypatch.setattr(config, "m3_core_rs", object(), raising=False)
    with caplog.at_level("WARNING", logger="memory.embed"):
        mod = _reimport_embed()
    assert mod._EMBEDDED_BREAKER is None
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "no CircuitBreaker" in msg, msg
    assert "DISABLED" in msg, msg
    assert "object" in msg, "the warning must name what was found"


def test_a_none_core_stays_quiet(monkeypatch, restore_embed, caplog):
    """The expected path must not cry wolf, or the warning above loses meaning."""
    from memory import config

    monkeypatch.setattr(config, "m3_core_rs", None, raising=False)
    with caplog.at_level("WARNING", logger="memory.embed"):
        _reimport_embed()
    assert not [r for r in caplog.records if "CircuitBreaker" in r.getMessage()]


def test_import_survives_a_none_core(monkeypatch, restore_embed):
    from memory import config

    monkeypatch.setattr(config, "m3_core_rs", None, raising=False)
    mod = _reimport_embed()
    assert mod._EMBEDDED_BREAKER is None


def test_a_real_core_still_builds_a_breaker(monkeypatch, restore_embed):
    """The guard must not disable breakers for a core that HAS the attribute."""
    built = []

    class _Core:
        @staticmethod
        def CircuitBreaker(threshold, reset_after_secs):  # noqa: N802 - mirrors the Rust name
            built.append((threshold, reset_after_secs))
            return f"breaker<{threshold}>"

    from memory import config

    monkeypatch.setattr(config, "m3_core_rs", _Core, raising=False)
    monkeypatch.setattr(config, "EMBED_BREAKER_EMBEDDED_THRESHOLD", 3, raising=False)
    mod = _reimport_embed()
    assert mod._EMBEDDED_BREAKER == "breaker<3>", mod._EMBEDDED_BREAKER
    assert built, "a real core must still produce a breaker"
