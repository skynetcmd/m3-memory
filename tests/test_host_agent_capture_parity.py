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
import re
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
    # opencode and aider were exempt here until 2026-09-20. Both are now
    # implemented against real local installs (OpenCode 1.18.0 SQLite +
    # legacy JSON, Aider 0.86.1 markdown), so the exemptions are gone and the
    # parametrized sweep covers them like any other agent.
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
        # A synthetic agent, NOT a real one: every registered agent is now
        # supported, so naming one here would make this test go green the day
        # that agent gets fixed rather than when the probe stops working.
        host_agents = {a: chatlog_config.HookSpec(enabled=True)
                       for a in ("claude-code", "some-unwired-agent")}

    chatlog_config.resolve_config = lambda *a, **k: _Cfg()
    try:
        findings = ep.check()["findings"]
    finally:
        chatlog_config.resolve_config = orig

    gaps = [f for f in findings if f["kind"] == "claimed_not_supported"]
    assert any(f["event"] == "some-unwired-agent" for f in gaps), (
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


# ── Aider: markdown, not JSON ────────────────────────────────────────────────

def test_the_aider_parser_separates_turns_from_console_output():
    """⚠ `>` LINES ARE NOT CONVERSATION.

    Aider's history is markdown, and its own console output — warnings, the
    invoking command line, token counts — is written as blockquotes. Capturing
    those would file a UnicodeDecodeError warning as a user turn. Grammar
    confirmed against Aider's io.py append_chat_history on main, and against a
    real 2,434-line history from 0.86.1.
    """
    raw = "\n".join([
        "",
        "# aider chat started at 2026-01-11 11:59:16",
        "",
        "> Terminal does not support pretty output (UnicodeDecodeError)  ",
        "> Aider v0.86.1  ",
        "",
        "#### how do I enable the repo map?  ",
        "",
        "Aider disables repo-map by default because:",
        "- It can slow down startup.",
        "",
        "```bash",
        "aider --repo-map",
        "```",
        "",
        "> Tokens: 617 sent, 8 received.  ",
    ])
    items, session = chatlog_ingest._parse_aider(raw)

    assert session == "2026-01-11 11:59:16"
    assert [i["role"] for i in items] == ["user", "assistant"]
    assert "repo map" in items[0]["content"]
    assert "```bash" in items[1]["content"], "fenced code was dropped from the reply"
    assert not any(i["content"].lstrip().startswith(">") for i in items), (
        "console output was captured as conversation"
    )
    assert not any("Tokens:" in i["content"] for i in items)


def test_a_multi_line_aider_user_message_is_one_turn():
    """⚠ AIDER REPEATS THE MARKER PER LINE.

    io.py joins the lines of ONE user message with "  \n#### ", so consecutive
    #### lines are a single turn. Treating each as its own turn would shatter a
    pasted stack trace into a dozen one-line 'messages'. This case is absent
    from the local sample, so it comes from the writer's source.
    """
    raw = "\n".join([
        "# aider chat started at 2026-01-11 12:00:00",
        "#### first line of the paste  ",
        "#### second line of the paste  ",
        "#### third line  ",
        "",
        "Understood.",
    ])
    items, _ = chatlog_ingest._parse_aider(raw)
    assert [i["role"] for i in items] == ["user", "assistant"], (
        f"expected one user turn and one reply, got {[i['role'] for i in items]}"
    )
    assert items[0]["content"].count("line") == 3


def test_aider_turns_are_attributed_to_their_own_session():
    """One file holds every run, so turns must not merge across sessions."""
    raw = "\n".join([
        "# aider chat started at 2026-01-11 11:00:00",
        "#### first question",
        "",
        "first answer",
        "",
        "# aider chat started at 2026-01-11 12:00:00",
        "#### second question",
        "",
        "second answer",
    ])
    items, session = chatlog_ingest._parse_aider(raw)
    convs = {i["conversation_id"] for i in items}
    assert len(convs) == 2, f"turns from two runs collapsed into {convs}"
    assert session == "2026-01-11 12:00:00", "session id is not the most recent run"


# ── OpenCode: a store, not a transcript ──────────────────────────────────────

def test_opencode_is_a_path_parser_not_a_text_parser():
    """OpenCode writes no transcript file, so its parser takes the STORE PATH.

    Registered in _PATH_PARSERS so _ingest hands it the path and does not
    os.path.isfile() a directory out of existence.
    """
    assert "opencode" in chatlog_ingest._PATH_PARSERS
    for fmt in chatlog_ingest._PATH_PARSERS:
        assert fmt in chatlog_ingest.PARSERS, (
            f"_PATH_PARSERS names '{fmt}', which is not a registered format"
        )


def test_the_opencode_reader_prefers_sqlite_but_honours_an_explicit_legacy_path(tmp_path):
    """⚠ AN EXPLICIT PATH MUST WIN.

    OpenCode moved sessions into opencode.db in v1.2.0 (Feb 2026), keeping the
    per-file JSON tree as legacy. Probing upward for the database from a legacy
    message directory hijacked the caller's choice and returned a DIFFERENT
    session's turns — silently capturing the wrong conversation.
    """
    import json as _json

    root = tmp_path / "opencode"
    msg_dir = root / "storage" / "message" / "ses_legacy"
    part_dir = root / "storage" / "part" / "msg_1"
    msg_dir.mkdir(parents=True)
    part_dir.mkdir(parents=True)
    (msg_dir / "msg_1.json").write_text(_json.dumps({
        "id": "msg_1", "sessionID": "ses_legacy", "role": "user",
        "time": {"created": 1767237681693},
        "model": {"providerID": "xai", "modelID": "grok-3-mini"},
    }), encoding="utf-8")
    (part_dir / "prt_1.json").write_text(_json.dumps({
        "id": "prt_1", "messageID": "msg_1", "type": "text",
        "text": "legacy turn",
    }), encoding="utf-8")
    # ⚠ A REAL DATABASE WITH A DIFFERENT SESSION IN IT. An unopenable stub
    # would make this test pass for the wrong reason: the probe would fail to
    # read it and fall through to the legacy tree even when the hijack bug is
    # present. Planting the bug must actually flip this test.
    import sqlite3 as _sq
    con = _sq.connect(root / "opencode.db")
    con.executescript(
        "CREATE TABLE session (id TEXT, time_created INTEGER, time_updated INTEGER);"
        "CREATE TABLE message (id TEXT, session_id TEXT, time_created INTEGER, data TEXT);"
        "CREATE TABLE part (id TEXT, message_id TEXT, session_id TEXT,"
        " time_created INTEGER, data TEXT);"
    )
    con.execute("INSERT INTO session VALUES ('ses_sqlite', 1, 2)")
    con.execute("INSERT INTO message VALUES ('m9','ses_sqlite',1,?)",
                (_json.dumps({"role": "user", "time": {"created": 1767237681693}}),))
    con.execute("INSERT INTO part VALUES ('p9','m9','ses_sqlite',1,?)",
                (_json.dumps({"type": "text", "text": "sqlite turn"}),))
    con.commit()
    con.close()

    items, session = chatlog_ingest._parse_opencode(str(msg_dir))
    assert session == "ses_legacy", (
        "an explicit legacy path was overridden by the sibling database"
    )
    assert [i["content"] for i in items] == ["legacy turn"]


def test_the_opencode_model_field_is_read_from_both_shapes():
    """⚠ THE FIELD MOVES BY ROLE — verified on a live 1.18.0 install.

    A user message carries nested `model: {providerID, modelID}`; an assistant
    message carries flat `providerID`/`modelID` alongside `tokens`. Reading one
    shape attributes half the turns to 'unknown'.
    """
    user = chatlog_ingest._opencode_item(
        {"role": "user", "model": {"providerID": "xai", "modelID": "grok-3-mini"}},
        "m1", "s1", ["hi"], 1767237681693)
    asst = chatlog_ingest._opencode_item(
        {"role": "assistant", "providerID": "xai", "modelID": "grok-3-mini-fast",
         "tokens": {"input": 12, "output": 34}},
        "m2", "s1", ["hello"], 1767237681702)

    assert user["model_id"] == "grok-3-mini" and user["provider"] == "xai"
    assert asst["model_id"] == "grok-3-mini-fast" and asst["provider"] == "xai"
    assert (asst["tokens_in"], asst["tokens_out"]) == (12, 34)
    assert user["timestamp"].startswith("2026-"), (
        "epoch MILLISECONDS were treated as seconds"
    )


def test_an_opencode_turn_with_no_text_part_is_skipped():
    """Tool-only and errored turns carry no conversation."""
    assert chatlog_ingest._opencode_item(
        {"role": "assistant", "modelID": "m"}, "m1", "s1", [], 0) is None
    assert chatlog_ingest._opencode_item(
        {"role": "assistant", "modelID": "m"}, "m1", "s1", ["  "], 0) is None


def test_an_unknown_opencode_provider_is_normalised():
    """⚠ providerID NAMES THE ROUTE, NOT ALWAYS THE VENDOR.

    A session through OpenCode's own gateway reports providerID="opencode",
    which is not in the chatlog's vendor enum — every turn in a live database
    failed to write with "provider must be one of [...]". Normalised to "other"
    rather than widening the enum, which would make `provider` answer two
    different questions.
    """
    from chatlog_config import VALID_PROVIDERS

    routed = chatlog_ingest._opencode_item(
        {"role": "user", "providerID": "opencode", "modelID": "qwen3.6-plus-free"},
        "m1", "s1", ["hi"], 1767237681693)
    assert routed["provider"] == "other", (
        f"unmapped provider {routed['provider']!r} would be rejected by the "
        f"chatlog schema"
    )
    assert routed["model_id"] == "qwen3.6-plus-free", "the model must survive"

    vendor = chatlog_ingest._opencode_item(
        {"role": "user", "providerID": "xai", "modelID": "grok-3-mini"},
        "m2", "s1", ["hi"], 1767237681693)
    assert vendor["provider"] == "xai", "a real vendor must not be flattened"


@pytest.mark.parametrize("fmt", ["aider", "opencode", "openclaw"])
def test_every_new_parser_emits_a_writable_provider(fmt):
    """The schema rejects anything outside the enum, and a rejected turn is
    lost capture. Assert the contract at the parser boundary rather than
    discovering it from a failed write."""
    from chatlog_config import VALID_PROVIDERS

    samples = {
        "aider": lambda: chatlog_ingest._parse_aider(
            "# aider chat started at 2026-01-11 11:00:00\n#### q\n\na\n")[0],
        "openclaw": lambda: chatlog_ingest._parse_openclaw(
            '{"type":"session","sessionId":"s"}\n'
            '{"id":"m","type":"message","message":{"role":"user","content":"q"}}\n')[0],
        "opencode": lambda: [chatlog_ingest._opencode_item(
            {"role": "user", "providerID": "some-new-router", "modelID": "m"},
            "m1", "s1", ["q"], 1767237681693)],
    }
    for item in samples[fmt]():
        assert item["provider"] in VALID_PROVIDERS, (
            f"{fmt} emits provider={item['provider']!r}, which the chatlog "
            f"schema rejects — the turn would be silently lost"
        )


# ── the hooks must invoke flags that exist ───────────────────────────────────

_HOOK_SCRIPTS = sorted(
    p for p in os.listdir(_HOOKS)
    if p.endswith((".sh", ".ps1")) and not p.startswith("_")
)


@pytest.mark.parametrize("script", _HOOK_SCRIPTS)
def test_a_hook_only_passes_ingest_flags_that_exist(script):
    """⚠ THE ROOT CAUSE OF THREE BROKEN AGENTS.

    Every one of these shipped for months invoking chatlog_ingest with an
    argument argparse rejects, so the hook exited 2 on every fire and capture
    silently never ran:

      * aider    `--watch <repo>`        — no such flag, ever
      * opencode `--format opencode`     — no such format, and no
                                           --transcript-path, which is required
      * openclaw (no hook at all)

    argparse exits 2 with an EMPTY STDOUT, which the hooks' own run_ingest
    reads as "m3 unreachable" — so the failure even pointed at the wrong layer.
    """
    with open(os.path.join(_HOOKS, script), encoding="utf-8") as fh:
        text = fh.read()
    # Only the lines that actually invoke ingest: a script also passes flags to
    # python, to its own CLI, and mentions others in comments.
    invocations = [ln for ln in text.splitlines()
                   if "chatlog_ingest.py" in ln and not ln.lstrip().startswith("#")]
    if not invocations:
        pytest.skip("does not invoke chatlog_ingest directly")

    with open(os.path.join(_BIN, "chatlog_ingest.py"), encoding="utf-8") as fh:
        known = set(re.findall(r'add_argument\(\s*"(--[a-z0-9-]+)"', fh.read()))
    assert known, "could not determine chatlog_ingest's accepted flags"

    used = {f for ln in invocations for f in re.findall(r'(--[a-z0-9-]{2,})', ln)}
    unknown = sorted(f for f in used if f not in known)
    assert not unknown, (
        f"{script} passes chatlog_ingest flag(s) it does not accept: "
        f"{unknown}. argparse exits 2 with empty stdout, which the hook "
        f"misreads as 'm3 unreachable', so capture dies silently."
    )


@pytest.mark.parametrize("script", _HOOK_SCRIPTS)
def test_a_hook_passes_a_format_that_exists(script):
    """`--format <x>` must name a registered parser."""
    with open(os.path.join(_HOOKS, script), encoding="utf-8") as fh:
        text = fh.read()
    invocations = [ln for ln in text.splitlines()
                   if "chatlog_ingest.py" in ln and not ln.lstrip().startswith("#")]
    if not invocations:
        pytest.skip("does not invoke chatlog_ingest directly")
    for ln in invocations:
        for fmt in re.findall(r'--format\s+([a-z0-9-]+)', ln):
            assert fmt in chatlog_ingest.PARSERS, (
                f"{script} passes --format {fmt}, which argparse rejects "
                f"(known: {sorted(chatlog_ingest.PARSERS)})"
            )


def test_the_opencode_store_is_located_portably(monkeypatch, tmp_path):
    """⚠ §1: THREE OSes, NOT ONE CONVENTION.

    The shell wrapper originally hardcoded `~/.local/share/opencode`, which is
    the XDG default — right on Linux, wrong on a macOS install under
    ~/Library/Application Support, and only accidentally right on Windows. Two
    wrappers each deriving their own list is also how the platforms drift
    apart (§10a), so both now pass `auto` and the candidate list has one owner.
    """
    monkeypatch.delenv("OPENCODE_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))

    # An explicit override always wins, on every OS.
    override = tmp_path / "elsewhere"
    override.mkdir()
    monkeypatch.setenv("OPENCODE_DATA_DIR", str(override))
    assert chatlog_ingest.opencode_store_candidates() == [override], (
        "OPENCODE_DATA_DIR did not take precedence"
    )


def test_the_hooks_do_not_derive_their_own_opencode_path():
    """Both wrappers must defer to the single owner, not re-implement it."""
    for name in ("opencode_session_end.sh", "opencode_session_end.ps1"):
        with open(os.path.join(_HOOKS, name), encoding="utf-8") as fh:
            body = "\n".join(ln for ln in fh.read().splitlines()
                             if not ln.lstrip().startswith("#"))
        assert ".local/share" not in body and ".local\\share" not in body, (
            f"{name} hardcodes the XDG path again — that is one OS's "
            f"convention baked into a hook that runs on three"
        )
        assert "--transcript-path auto" in body, (
            f"{name} no longer defers store discovery to chatlog_ingest"
        )
