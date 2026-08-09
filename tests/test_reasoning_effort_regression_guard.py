"""Guard: every local chat/completions caller must suppress reasoning-model thinking.

THE FAILURE MODE
A reasoning model (Qwen3.5, DeepSeek-R1, gpt-oss, gemma-with-thinking — rapidly
becoming the local default) spends its token budget in the `reasoning` channel
and returns `finish_reason="length"` with an EMPTY `content`. A caller that reads
`choices[0].message.content` then gets "" and treats it as "the model had nothing
to say". Nothing errors. Nothing retries. The feature silently degrades to a
no-op that looks like a clean result.

Measured 2026-08-09 on the citation-drift judge: with qwen3.5:9b it abstained on
100% of syntheses, so `m3 wiki generate --check-drift` reported "0 findings"
while checking nothing. Turning thinking off took it to recall 1.00 /
precision 1.00 on the same fixture.

THE MITIGATION
`llm_failover.suppresses_thinking_via_effort(url)` -> send
`reasoning_effort="none"`. Both LM Studio and Ollama honor it (verified live);
it is gated to those local runtimes because "none" 400s on real OpenAI cloud.

WHY A GUARD AND NOT JUST A FIX
The mitigation is opt-in per call site, so it is invisible to forget: a new
caller that omits it works perfectly on a non-reasoning model and silently
returns nothing on a reasoning one. That is exactly the shape this repo already
guards with test_no_window_regression_guard.py, so this mirrors it.

RATCHET, NOT A BIG BANG. `KNOWN_GAPS` enumerates callers that predate the
mitigation. The guard asserts the set never GROWS, and fails if an entry is
fixed but not removed — so the debt is visible, shrinking, and cannot silently
come back.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"

_HELPER = "suppresses_thinking_via_effort"

# Cloud-only callers: "none" is NOT valid on real OpenAI/Anthropic/xAI and would
# 400. These must NOT adopt the mitigation, so they are excluded by design
# rather than listed as debt.
_CLOUD_ONLY = {
    "grok_bridge.py",
    "web_research_bridge.py",
    "unified_ai.py",
    "custom_tool_bridge.py",
    "debug_agent_bridge.py",
    "mcp_proxy.py",
    "agent_protocol.py",
    "batch_runner.py",
}

# Dev scripts / probes that live in bin/ but are not product call paths.
_NOT_PRODUCT = {
    "test_unified_router.py",
    "test_mcp_proxy.py",
}

# Callers that talk to a LOCAL endpoint and still lack the mitigation. Shrink
# this list; never add to it. Each is a place a reasoning model silently
# degrades the feature today.
KNOWN_GAPS = {
    "memory_core.py",
    "memory_maintenance.py",
    "promote_pipeline.py",
    "memory/enrich.py",
    "files_memory/summarize.py",
    "files_memory/config.py",
    "dashboard/health.py",
}


def _chat_completion_callers() -> set[str]:
    """Modules under bin/ that POST to a chat/completions endpoint."""
    out: set[str] = set()
    for path in _BIN.rglob("*.py"):
        if "__pycache__" in path.parts or ".venv" in path.parts:
            continue
        try:
            src = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "chat/completions" not in src:
            continue
        rel = str(path.relative_to(_BIN))
        if path.name in _CLOUD_ONLY or path.name in _NOT_PRODUCT:
            continue
        out.add(rel)
    return out


def _uses_helper(rel: str) -> bool:
    src = (_BIN / rel).read_text(encoding="utf-8", errors="ignore")
    return _HELPER in src


def test_no_new_reasoning_effort_gaps():
    """A NEW local chat caller must route through the shared mitigation."""
    offenders = {r for r in _chat_completion_callers() if not _uses_helper(r)}
    new = offenders - KNOWN_GAPS
    assert not new, (
        f"{sorted(new)} POST to a local chat/completions endpoint without "
        f"{_HELPER}(). On a reasoning model these return an empty `content` and "
        f"the feature silently no-ops. Send reasoning_effort=\"none\" via the "
        f"shared helper (see bin/wiki/citation_drift.py::_call_model), or add "
        f"the module to _CLOUD_ONLY if it only ever talks to a cloud API."
    )


def test_known_gaps_list_is_accurate():
    """A fixed gap must be REMOVED from the list, so the debt stays honest and
    a regression cannot hide behind a stale allowlist."""
    offenders = {r for r in _chat_completion_callers() if not _uses_helper(r)}
    stale = KNOWN_GAPS - offenders
    assert not stale, (
        f"{sorted(stale)} now use {_HELPER} (or no longer call chat/completions) "
        f"but are still listed in KNOWN_GAPS — delete them from the list so the "
        f"ratchet keeps tightening."
    )


@pytest.mark.parametrize("rel", sorted([
    "wiki/citation_drift.py",
    "wiki/synth.py",
    "wiki/prose_compiler.py",
    "slm_intent.py",
    "m3_entities.py",
    "files_memory/extract.py",
]))
def test_known_good_callers_keep_the_mitigation(rel):
    """Pin the callers that HAVE it, so a refactor cannot quietly drop it."""
    assert (_BIN / rel).is_file(), f"{rel} moved — update this guard"
    assert _uses_helper(rel), (
        f"{rel} lost its {_HELPER}() call. A reasoning model will return empty "
        f"content and this caller will silently degrade to a no-op."
    )


def test_helper_is_gated_to_local_runtimes():
    """The mitigation must stay OFF for cloud endpoints: reasoning_effort="none"
    is non-standard and 400s on real OpenAI."""
    import sys
    sys.path.insert(0, str(_BIN))
    from llm_failover import suppresses_thinking_via_effort as f

    assert f("http://127.0.0.1:1234/v1/chat/completions") is True   # LM Studio
    assert f("http://127.0.0.1:11434/v1/chat/completions") is True  # Ollama
    assert f("https://api.openai.com/v1/chat/completions") is False


def test_guard_covers_the_module_that_regressed():
    """citation_drift is why this guard exists — it must be in scope, not
    filtered out by the cloud-only/dev exclusions."""
    assert "wiki/citation_drift.py" in _chat_completion_callers()


def test_ast_parse_of_flagged_modules(tmp_path):
    """Sanity: every module the guard inspects is real, parseable Python, so a
    silent read error can never make the offender set look empty."""
    callers = _chat_completion_callers()
    assert callers, "found no chat/completions callers — the guard is not looking"
    for rel in callers:
        ast.parse((_BIN / rel).read_text(encoding="utf-8", errors="ignore"))
