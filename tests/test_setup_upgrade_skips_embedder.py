"""On an upgrade (native embedder already installed), the wizard skips the
embedder prompt but still asks the LLM-endpoint question.

Re-running setup over an existing install previously re-asked "Install the
Project Oxidation native wheel?" — noise, since the decision was already made.
_gather_plan now detects active_embedder_tier()["native"] and keeps the embedder
without prompting. The LLM-endpoint probe is unaffected.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_wizard(monkeypatch):
    # Import under the real package name so dataclass field resolution works
    # (loading the file under a synthetic module name breaks @dataclass InitVar
    # lookups via cls.__module__).
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from m3_memory import setup_wizard  # noqa: PLC0415
    return setup_wizard


def _make_args():
    # Interactive path: non_interactive=False so _gather_plan runs the prompts.
    # Include every attribute _gather_plan reads so no AttributeError short-circuits.
    return argparse.Namespace(
        endpoint=None, capture_mode=None, install_gpu_embedder=False,
        no_native_wheel=False, allow_native_source_build=False,
        decouple_roots=False, config_root=None, engine_root=None,
        yes=False, non_interactive=False, cognitive_loop=False, agents=None,
        fips_mode=False, fips_strict=False, install_wolfssl=False,
        no_governor_migration=False,
    )


def _run_gather(monkeypatch, *, native: bool):
    wiz = _load_wizard(monkeypatch)
    asked: list[str] = []

    # Record every yes/no question; always answer the default (return it).
    def fake_ask(question, default=False):
        asked.append(question)
        return default

    monkeypatch.setattr(wiz, "_ask_yes_no", fake_ask)
    # Some prompts use raw input() (e.g. capture-mode choice); feed defaults so
    # the flow reaches the embedder block instead of blocking on stdin.
    monkeypatch.setattr("builtins.input", lambda *a, **k: "", raising=False)
    # Force the interactive branch + stub the embedder-tier probe.
    monkeypatch.setattr(wiz.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(
        "m3_memory.rust_core_install.active_embedder_tier",
        lambda: {"native": native, "backend": None, "version": None, "summary": ""},
        raising=False,
    )
    # Stub the LLM probe + any side-effecting calls so _gather_plan stays pure.
    monkeypatch.setattr(wiz, "_probe_llm_endpoints", lambda *a, **k: None, raising=False)

    detected = wiz.AgentTargets()  # nothing detected → fewer prompts to wade through
    try:
        wiz._gather_plan(detected, _make_args())
    except Exception as e:  # noqa: BLE001
        # Steps AFTER the embedder prompt (root-decouple, governor, etc.) may
        # touch the filesystem/env and raise under the stubs — that's fine, we
        # only assert on which questions were asked up to that point. But a crash
        # BEFORE any question means the harness is wrong, not the code.
        assert asked, f"_gather_plan raised before any prompt: {type(e).__name__}: {e}"
    return asked


def _embedder_asked(asked):
    # The fresh-install embedder prompt offers the OPTIONAL tier-1 native wheel
    # (shared tier-2 is the default and isn't prompted). Match its wording
    # robustly — "tier-1", "native ... wheel", or "oxidation".
    def _hit(q):
        ql = q.lower()
        return ("oxidation" in ql or "tier-1" in ql or "tier 1" in ql
                or ("native" in ql and "wheel" in ql))
    return any(_hit(q) for q in asked)


def test_upgrade_skips_embedder_prompt(monkeypatch):
    asked = _run_gather(monkeypatch, native=True)
    assert not _embedder_asked(asked), (
        "embedder prompt should be skipped when the native wheel is already installed"
    )


def test_fresh_install_asks_embedder_prompt(monkeypatch):
    asked = _run_gather(monkeypatch, native=False)
    assert _embedder_asked(asked), (
        "embedder prompt should be shown on a fresh install (no native wheel yet)"
    )
