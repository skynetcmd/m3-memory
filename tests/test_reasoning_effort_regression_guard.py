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

# Either symbol satisfies the guard: the predicate is the original seam, the
# apply_* wrapper is the one-liner that replaced hand-rolled try/except blocks.
_HELPERS = ("apply_thinking_suppression", "suppresses_thinking_via_effort")
_HELPER = _HELPERS[0]

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
KNOWN_GAPS: set[str] = set()


def _executable_source(src: str) -> str:
    """Source with comments and docstrings removed.

    Both are legitimate places to NAME an endpoint — files_memory/config.py
    documents the `/v1/v1/chat/completions` bug at length and never posts
    anything. A raw substring scan called it a caller. Same technique
    test_model_discovery_no_hardcoded_names.py uses for the same reason.
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body.pop(0)          # drop the docstring
    try:
        return ast.unparse(tree)  # comments are already gone via the AST
    except Exception:             # noqa: BLE001
        return src


def _chat_completion_callers() -> set[str]:
    """Modules under bin/ that actually POST to a chat/completions endpoint.

    Requires BOTH a real `.post(` call and a chat/completions reference in
    EXECUTABLE source — a module that merely documents or builds the URL is not
    a caller and must not be listed as debt.
    """
    out: set[str] = set()
    for path in _BIN.rglob("*.py"):
        if "__pycache__" in path.parts or ".venv" in path.parts:
            continue
        try:
            src = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if ".post(" not in src:
            continue
        code = _executable_source(src)
        if ".post(" not in code:
            continue
        sends_messages = '"messages"' in code or "'messages'" in code
        names_endpoint = "chat/completions" in code
        if not (sends_messages or names_endpoint):
            continue
        if path.name in _CLOUD_ONLY or path.name in _NOT_PRODUCT:
            continue
        # as_posix(), not str(): on Windows str() yields "wiki\citation_drift.py"
        # while every assertion in this file spells the forward-slash form, so
        # the guard silently missed nested modules on windows-latest (§1 — the
        # suite must behave identically on all three OSes).
        out.add(path.relative_to(_BIN).as_posix())
    return out


def _chat_post_functions(src: str):
    """Yield (name, node) for each OUTERMOST function containing a `.post(`
    call that passes `json=`.

    Outermost, not every nested def: m3_entities resolves the suppression
    predicate ONCE in the enclosing function and applies it inside a closure —
    efficient and correct, but a per-def check reads the closure as unprotected.
    The outermost unit is the smallest scope that contains both halves of that
    pattern while still isolating sibling call sites (memory/enrich.py's three
    chat calls live in three separate top-level functions and stay separate).
    """
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return
    _FN = (ast.FunctionDef, ast.AsyncFunctionDef)
    inner: set[int] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, _FN):
            continue
        for sub in ast.walk(fn):
            if sub is not fn and isinstance(sub, _FN):
                inner.add(id(sub))
    for fn in ast.walk(tree):
        if not isinstance(fn, _FN) or id(fn) in inner:
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "post"
                    and any(k.arg == "json" for k in node.keywords)):
                yield fn.name, fn
                break


def _unprotected_functions(rel: str) -> list[str]:
    """Functions that POST a json payload without the mitigation anywhere in
    them.

    Per-FUNCTION, not per-file: memory/enrich.py has three separate chat calls,
    and a file-level substring check passed while two of them were unprotected
    (caught by sabotaging one site — the guard stayed green). Function scope is
    the smallest unit that still tolerates the two legitimate shapes: wrapping
    the dict inline, and mutating a `payload` variable before the post.
    """
    src = (_BIN / rel).read_text(encoding="utf-8", errors="ignore")
    bad = []
    for name, fn in _chat_post_functions(src):
        seg = ast.unparse(fn)
        protected = (any(h in seg for h in _HELPERS)
                     or "reasoning_effort" in seg)  # set directly, e.g. m3_entities
        if not protected:
            bad.append(name)
    return bad


def _uses_helper(rel: str) -> bool:
    """True when every chat-posting function in the module is protected."""
    src = (_BIN / rel).read_text(encoding="utf-8", errors="ignore")
    if not any(h in src for h in _HELPERS):
        return False
    return not _unprotected_functions(rel)


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
    "wiki/derivability_review.py",
    "slm_intent.py",
    "m3_entities.py",
    "files_memory/extract.py",
    "files_memory/summarize.py",
    "files_memory/carry_forward.py",
    "memory_core.py",
    "memory/enrich.py",
    "memory_maintenance.py",
    "run_observer.py",
    "run_reflector.py",
]))
def test_known_good_callers_keep_the_mitigation(rel):
    """Pin the callers that HAVE it, so a refactor cannot quietly drop it."""
    assert (_BIN / rel).is_file(), f"{rel} moved — update this guard"
    unprotected = _unprotected_functions(rel)
    assert _uses_helper(rel), (
        f"{rel} has chat-posting function(s) without {_HELPER}(): "
        f"{unprotected or '(module-level reference missing)'}. A reasoning model "
        f"will return empty content and these will silently degrade to no-ops."
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
