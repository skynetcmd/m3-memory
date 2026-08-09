"""A truncated model reply must not be recorded as an empty result.

"The model looked and found nothing" and "the model never got to answer" are
different facts. Every caller that maps both to `[]` reports the second as the
first — which is how a reasoning model turned the citation-drift judge into a
permanent "0 findings" (2026-08-09) while the run looked clean.

`llm_failover.reply_ran_out_of_room()` is the one place that distinguishes them,
across BOTH wire formats the observer/reflector speak:
  * OpenAI chat     -> finish_reason == "length"
  * Anthropic msgs  -> stop_reason  == "max_tokens"

Hermetic: no server, no database, no platform branch — pure response inspection,
so it behaves identically on macOS, Windows and Linux and is indifferent to
whether the store is SQLite or PostgreSQL.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

from llm_failover import (  # noqa: E402
    apply_thinking_suppression,
    apply_thinking_suppression_anthropic,
    reply_ran_out_of_room,
)

_LOCAL_OAI = "http://127.0.0.1:1234/v1/chat/completions"
_LOCAL_ANTHROPIC = "http://127.0.0.1:1234/v1/messages"
_CLOUD = "https://api.anthropic.com/v1/messages"


# ── the distinction itself ────────────────────────────────────────────────

@pytest.mark.parametrize("text,stop,expected", [
    # Truncated with nothing usable -> the error case, both wire formats.
    ("", "length", True),
    ("", "max_tokens", True),
    ("   ", "length", True),
    ("\n\t", "max_tokens", True),
    # Truncated but we DID get text: partial output is still output; the caller
    # parses what it has rather than throwing the work away.
    ("{'observations': []}", "length", False),
    ("partial", "max_tokens", False),
    # Empty because the model genuinely had nothing to say — NOT an error, and
    # the case that must keep flowing through as a legitimate empty result.
    ("", "stop", False),
    ("", "end_turn", False),
    ("", None, False),
    ("", "", False),
])
def test_only_truncated_and_empty_is_an_error(text, stop, expected):
    assert reply_ran_out_of_room(text, stop) is expected


def test_unknown_stop_reason_is_not_treated_as_truncation():
    """Fail SAFE: an unrecognised stop reason must not manufacture an error and
    abort a working pipeline on a server whose vocabulary we do not know."""
    assert reply_ran_out_of_room("", "content_filter") is False
    assert reply_ran_out_of_room("", "tool_use") is False


# ── suppression is per-wire-format and local-only ─────────────────────────

def test_openai_wire_uses_reasoning_effort():
    p = apply_thinking_suppression({}, _LOCAL_OAI)
    assert p["reasoning_effort"] == "none"
    assert "thinking" not in p


def test_anthropic_wire_uses_thinking_disabled():
    """The Messages API has no reasoning_effort; sending one would be ignored at
    best and rejected at worst."""
    p = apply_thinking_suppression_anthropic({}, _LOCAL_ANTHROPIC)
    assert p["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in p


def test_neither_suppression_reaches_a_cloud_endpoint():
    """Cloud Anthropic already defaults thinking OFF, and older
    anthropic-version values reject unknown params — so sending it upstream buys
    nothing and risks a 400."""
    assert apply_thinking_suppression_anthropic({}, _CLOUD) == {}
    assert apply_thinking_suppression({}, "https://api.openai.com/v1/chat/completions") == {}


def test_suppression_preserves_the_existing_payload():
    payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    out = apply_thinking_suppression_anthropic(payload, _LOCAL_ANTHROPIC)
    assert out is payload                      # mutates in place, returns for chaining
    assert out["model"] == "m" and out["messages"]


def test_suppression_never_raises_on_a_junk_url():
    """A suppression hint is an optimisation; it must never be the reason a call
    fails."""
    for bad in ("", "not-a-url", "http://[::bad", None):
        assert isinstance(apply_thinking_suppression({}, bad), dict)
        assert isinstance(apply_thinking_suppression_anthropic({}, bad), dict)


# ── the callers actually enforce it ───────────────────────────────────────

@pytest.mark.parametrize("mod", ["run_observer.py", "run_reflector.py"])
def test_both_wire_branches_check_for_truncation(mod):
    """Both modules speak both wires; a guard on only one branch leaves the
    other silently degrading."""
    src = (_BIN / mod).read_text(encoding="utf-8")
    # count CALL sites: the import line has no trailing "("
    assert src.count("reply_ran_out_of_room(") == 2, (
        f"{mod} must check truncation in BOTH the anthropic and openai branches")
    assert "apply_thinking_suppression_anthropic" in src
    assert "apply_thinking_suppression(" in src
