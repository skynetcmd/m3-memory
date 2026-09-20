"""A registered host agent must actually be capturable, end to end.

⚠ WHY THIS EXISTS. A user installed m3 on a new machine and reported that
chatlog capture was not wired for OpenClaw, and that `doctor --fix` with and
without `--fix-hooks` never addressed it. Both halves were true, and the
investigation found the same shape five times over:

  * `VALID_HOST_AGENTS` was duplicated verbatim in chatlog_config AND
    chatlog_core, and `ChatlogConfig.host_agents` restated it a third time.
  * `chatlog_ingest.PARSERS` had 3 of the 6 registered agents, so
    `--format opencode` — which bin/hooks/chatlog/opencode_session_end.py
    passes on every session end — was rejected by argparse. OpenCode capture
    had never worked either.
  * `chatlog_init.get_hook_path_for_agent` had a 5-agent map with an
    `("unknown", "unknown hook")` fallback, so `langchain` and `openclaw`
    resolved to a path that does not exist instead of erroring.

Every one of those fails as SILENTLY MISSING CAPTURE rather than as an error,
which is the §3 failure mode this file exists to convert into a red test.

The rule: registering an agent is a claim of support. This asserts the claim.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_BIN = os.path.join(_ROOT, "bin")
_HOOKS = os.path.join(_BIN, "hooks", "chatlog")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import chatlog_config  # noqa: E402
import chatlog_core  # noqa: E402
import chatlog_ingest  # noqa: E402
import chatlog_init  # noqa: E402


# Agents whose capture is genuinely not implemented yet. An entry here is a
# DECLARED gap with a reason, which is the opposite of the silent omission this
# file was written after: it still shows up, it just does not fail the build.
_KNOWN_GAPS = {
    "langchain": (
        "library integration, not a CLI with a session lifecycle — turns are "
        "submitted by the embedding application, so there is no hook to wire"
    ),
    "opencode": (
        "hook passes --format opencode but no parser exists, so capture has "
        "never worked. Needs a real OpenCode transcript to write a parser "
        "against; guessing its format is what produced this bug class"
    ),
    "aider": (
        "aider_chat_watcher.sh invokes `--format aider --watch <repo>` and "
        "NEITHER exists: there is no aider parser and no --watch mode at all. "
        "Aider writes Markdown (.aider.chat.history.md), not JSONL, so it needs "
        "its own parser plus a tail-mode runner — a larger change than adding a "
        "format. Found 2026-09-20 alongside the OpenClaw gap"
    ),
}


def test_the_agent_allowlist_has_exactly_one_owner():
    """⚠ THREE COPIES EXISTED. chatlog_core imported nothing and restated the
    frozenset; ChatlogConfig.host_agents hand-listed it again. A copy is the
    defect independent of whether it currently agrees (§10a) — drift here means
    the config accepts a value ingest rejects, and the user sees missing
    capture rather than a bad-config error."""
    assert chatlog_core.VALID_HOST_AGENTS is chatlog_config.VALID_HOST_AGENTS, (
        "chatlog_core.VALID_HOST_AGENTS is not the same object as "
        "chatlog_config's — it has been restated instead of imported"
    )
    default_keys = set(chatlog_config.ChatlogConfig().host_agents)
    assert default_keys == set(chatlog_config.VALID_HOST_AGENTS), (
        f"ChatlogConfig.host_agents drifted from VALID_HOST_AGENTS: "
        f"only-in-default={sorted(default_keys - set(chatlog_config.VALID_HOST_AGENTS))}, "
        f"only-in-allowlist={sorted(set(chatlog_config.VALID_HOST_AGENTS) - default_keys)}"
    )


@pytest.mark.parametrize("agent", sorted(chatlog_config.VALID_HOST_AGENTS))
def test_a_registered_agent_can_be_ingested(agent):
    """Every agent needs a parser, or `--format <agent>` is an argparse error.

    This is the defect the user hit: the OpenCode hook has shipped for months
    passing a --format value that chatlog_ingest rejects.
    """
    if agent in _KNOWN_GAPS:
        pytest.skip(f"known gap: {_KNOWN_GAPS[agent]}")
    assert agent in chatlog_ingest.PARSERS, (
        f"'{agent}' is a registered host agent with no parser in "
        f"chatlog_ingest.PARSERS, so `--format {agent}` fails argparse and "
        f"capture silently never runs. Add a parser, or declare it in "
        f"_KNOWN_GAPS with a reason."
    )


@pytest.mark.parametrize("agent", sorted(chatlog_config.VALID_HOST_AGENTS))
def test_a_registered_agent_has_a_hook_on_disk(agent):
    """get_hook_path_for_agent must not hand back a path to nothing.

    Its `("unknown", "unknown hook")` fallback returned
    bin/hooks/chatlog/unknown.sh for any unmapped agent — a file that has never
    existed. The wiring instructions then told the user to install it.
    """
    if agent in _KNOWN_GAPS:
        pytest.skip(f"known gap: {_KNOWN_GAPS[agent]}")
    sh_path, ps1_path, desc = chatlog_init.get_hook_path_for_agent(agent)
    assert "unknown" not in desc, (
        f"'{agent}' falls through get_hook_path_for_agent's unknown-hook "
        f"fallback, which yields {os.path.basename(sh_path)} — a file that "
        f"does not exist. Map it, or declare it in _KNOWN_GAPS."
    )
    assert os.path.exists(sh_path), f"{agent}: {sh_path} does not exist"
    assert os.path.exists(ps1_path), f"{agent}: {ps1_path} does not exist"


def test_every_known_gap_is_still_registered():
    """Keep the exemption list honest.

    A stale exemption is as bad as a missing one: it suppresses a test for an
    agent that has since been implemented (or removed), and nothing says so.
    """
    stale = sorted(set(_KNOWN_GAPS) - set(chatlog_config.VALID_HOST_AGENTS))
    assert not stale, (
        f"_KNOWN_GAPS names agent(s) that are no longer registered: {stale}. "
        f"Drop the exemption."
    )
    for agent in sorted(set(_KNOWN_GAPS) & set(chatlog_config.VALID_HOST_AGENTS)):
        if agent in chatlog_ingest.PARSERS:
            pytest.fail(
                f"'{agent}' now HAS a parser but is still exempted in "
                f"_KNOWN_GAPS — remove the exemption so it is really tested"
            )


def test_openclaw_is_registered_and_wired():
    """The specific regression the user reported.

    Named explicitly rather than left to the parametrized sweep: a future edit
    that drops openclaw from VALID_HOST_AGENTS would make the sweep pass by
    testing one fewer agent, which is the silent-omission failure again.
    """
    assert "openclaw" in chatlog_config.VALID_HOST_AGENTS
    assert "openclaw" in chatlog_ingest.PARSERS
    assert os.path.exists(os.path.join(_HOOKS, "openclaw_session_end.py"))


def test_openclaw_and_opencode_are_not_confused():
    """Different products, both supported, one letter apart in practice.

    Their transcript formats are NOT known to match, so sharing a parser would
    be a guess. If a future change points them at the same callable, that has
    to be a deliberate, verified decision rather than a typo.
    """
    oc = chatlog_ingest.PARSERS.get("opencode")
    ocl = chatlog_ingest.PARSERS.get("openclaw")
    if oc is not None and ocl is not None:
        assert oc is not ocl, (
            "opencode and openclaw share a parser. They are different products; "
            "if this is intentional, verify it against a real OpenCode "
            "transcript and say so here."
        )


def test_the_openclaw_parser_handles_a_real_transcript_shape():
    """Parse the exact JSONL shape a live OpenClaw install writes.

    Shape verified against OpenClaw 2026.3.28 at
    ~/.openclaw/agents/<agent>/sessions/<uuid>.jsonl — a `session` header, then
    `model_change` / `custom` / `message` records.
    """
    raw = "\n".join([
        '{"type":"session","sessionId":"sess-1","timestamp":"2026-09-20T00:00:00Z"}',
        '{"type":"model_change","model":"gpt-4o"}',
        '{"type":"custom","payload":{"note":"ignored"}}',
        '{"id":"m1","type":"message","timestamp":"2026-09-20T00:00:01Z",'
        '"message":{"role":"user","content":"how do I pin a memory?"}}',
        '{"id":"m2","type":"message","timestamp":"2026-09-20T00:00:02Z",'
        '"message":{"role":"assistant","content":[{"type":"text","text":"Use memory_pin."}]}}',
        '{"id":"m3","type":"message","message":{"role":"user","content":"/reset"}}',
        'not json at all',
    ])
    items, session_id = chatlog_ingest._parse_openclaw(raw)

    assert session_id == "sess-1"
    assert [i["role"] for i in items] == ["user", "assistant"], (
        "expected exactly the two real turns: the /reset command is control "
        "input, and the malformed line must be skipped without aborting"
    )
    assert items[1]["content"] == "Use memory_pin.", "list-of-parts content not flattened"
    assert all(i["model_id"] == "gpt-4o" for i in items), (
        "model_change did not attribute the turns that follow it"
    )
    assert all(i["conversation_id"] == "sess-1" for i in items)


def test_the_openclaw_parser_tolerates_an_empty_transcript():
    """A fresh install with no turns yet is a no-op, not a crash."""
    assert chatlog_ingest._parse_openclaw("") == ([], None)
    assert chatlog_ingest._parse_openclaw("   \n\n ") == ([], None)


# ── the doctor must see it ───────────────────────────────────────────────────

def test_doctor_reports_an_enabled_agent_it_cannot_capture():
    """⚠ THE SECOND HALF OF THE USER'S REPORT.

    environment_probe reads ONLY ~/.claude/settings.json, and its one
    corroboration check was hardcoded to claude-code. For a user on any other
    host it inspected an unrelated file, found nothing, and reported clean —
    including under `--fix --fix-hooks`, whose repair path is
    install_claude_settings. So "chatlog was never wired" and "doctor never
    mentioned it" were the same defect seen twice.
    """
    from doctor import environment_probe as ep

    orig = chatlog_config.resolve_config

    class _Cfg:
        host_agents = {a: chatlog_config.HookSpec(enabled=True)
                       for a in ("claude-code", "opencode")}

    chatlog_config.resolve_config = lambda *a, **k: _Cfg()
    try:
        findings = ep.check()["findings"]
    finally:
        chatlog_config.resolve_config = orig

    gaps = [f for f in findings if f["kind"] == "claimed_not_supported"]
    assert any(f["event"] == "opencode" for f in gaps), (
        "doctor stayed silent about an enabled agent whose capture cannot "
        "possibly run — the exact silence the user reported"
    )


def test_doctor_does_not_cry_wolf_about_a_working_agent():
    """§3: a false alarm is a violation in its own right.

    openclaw has a parser and a hook as of this change, so flagging it would
    train the user to ignore the finding that matters.
    """
    from doctor import environment_probe as ep

    orig = chatlog_config.resolve_config

    class _Cfg:
        host_agents = {"openclaw": chatlog_config.HookSpec(enabled=True)}

    chatlog_config.resolve_config = lambda *a, **k: _Cfg()
    try:
        findings = ep.check()["findings"]
    finally:
        chatlog_config.resolve_config = orig

    assert not [f for f in findings
                if f["kind"] == "claimed_not_supported" and f["event"] == "openclaw"], (
        "doctor flagged openclaw, which now has both a parser and a hook"
    )
