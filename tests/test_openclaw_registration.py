"""OpenClaw is wired as a NATIVE MCP server, version-gated at 2026.3.22.

OpenClaw shipped a native MCP client in 2026.3.22 (bisected against upstream
tags: 2026.3.11 has no src/config/mcp-config.ts, 2026.3.22 does). m3 therefore
registers a real stdio MCP server via `openclaw mcp set` instead of pointing the
client at the OpenAI-shape proxy on :9000. bin/mcp_proxy.py stays for Aider and
other non-MCP clients; it is simply no longer OpenClaw's path.

This suite pins the parts that were measured rather than assumed, and the parts
that fail silently if they regress:

  * argv[0] is the ABSOLUTE path shutil.which returned. Bare "openclaw" is an npm
    .CMD shim on Windows and CreateProcess raises FileNotFoundError [WinError 2].
  * the toolFilter is DERIVED from the bridge's startup set, never a literal.
  * the version gate TRIPS below the floor and, just as importantly, STAYS QUIET
    on a healthy version (§3: a false alarm is a violation, not a safe default).
  * an unreadable version banner does not raise — the repo's _parse_version
    raises TypeError on exactly the strings OpenClaw prints.
  * the failure path echoes the exact argv, and that argv carries no secrets.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

import m3_memory.setup_wizard as sw  # noqa: E402

_REAL_BANNER = "OpenClaw 2026.3.28 (f9b1079)"
_FAKE_EXE = "/usr/local/bin/openclaw"       # POSIX-shaped on purpose: the test
_FAKE_EXE_WIN = "C:/npm/openclaw.CMD"       # must not bake in a .CMD assumption


@pytest.fixture
def wired(monkeypatch):
    """Patch the four things _wire_openclaw touches; record every argv."""
    calls: list[list[str]] = []

    def _install(*, version: str = _REAL_BANNER, set_rc: int = 0,
                 exe: str | None = _FAKE_EXE, set_out: str = "saved"):
        monkeypatch.setattr(sw.shutil, "which", lambda name: exe)

        def fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            if "--version" in cmd:
                return SimpleNamespace(returncode=0, stdout=version, stderr="")
            return SimpleNamespace(returncode=set_rc, stdout=set_out, stderr="")

        monkeypatch.setattr(sw.subprocess, "run", fake_run)
        # _canonical_memory_env is imported lazily INSIDE the installer call, so
        # patch it on the installer module — patching sw would not take effect.
        import m3_memory.installer as inst
        monkeypatch.setattr(inst, "_canonical_memory_env",
                            lambda: {"M3_ENGINE_ROOT": "/data/e",
                                     "M3_CONFIG_ROOT": "/data/c"})
        return calls

    return _install


def _payload(calls: list[list[str]]) -> dict:
    """The JSON spec from the `mcp set` argv."""
    setc = [c for c in calls if "set" in c]
    assert setc, f"no `mcp set` call was made; calls={calls}"
    return json.loads(setc[-1][4])


# ── happy path ───────────────────────────────────────────────────────────────

def test_registers_with_absolute_exe_and_stdio_spec(wired):
    calls = wired()
    assert sw._wire_openclaw() is True

    setc = [c for c in calls if "set" in c][-1]
    # THE WinError-2 REGRESSION GUARD: argv[0] is what which() returned, never
    # the bare name. Asserted against the fixture value, not a literal ".CMD" —
    # macOS/Linux return an extensionless shim and the code must not care.
    assert setc[0] == _FAKE_EXE
    assert setc[1:4] == ["mcp", "set", "m3_memory"]

    spec = _payload(calls)
    assert spec["transport"] == "stdio"
    assert spec["enabled"] is True
    # OpenClaw's schema requires a non-empty command for stdio.
    assert spec.get("command"), f"stdio spec needs a command: {spec}"
    assert spec["env"]["M3_ENGINE_ROOT"] == "/data/e"
    assert spec["env"]["M3_CONFIG_ROOT"] == "/data/c"


def test_windows_cmd_shim_path_is_passed_through_verbatim(wired):
    """A .CMD path must round-trip unchanged (it is the real Windows case)."""
    calls = wired(exe=_FAKE_EXE_WIN)
    assert sw._wire_openclaw() is True
    assert [c for c in calls if "set" in c][-1][0] == _FAKE_EXE_WIN


def test_tool_filter_is_derived_from_the_bridge_startup_set(wired):
    """The filter must equal memory_bridge's startup surface, not a copy of it.

    Fails if either side drifts — which is the point. A hardcoded list here
    would silently diverge from _register_initial_tools().
    """
    import mcp_tool_catalog
    import memory_bridge
    import tool_domains

    meta = set(memory_bridge._META_TOOLS)
    expected = sorted({
        s.name for s in mcp_tool_catalog.TOOLS
        if s.name in meta or tool_domains.is_essential(s.name)
    })

    calls = wired()
    assert sw._wire_openclaw() is True
    include = _payload(calls)["toolFilter"]["include"]
    assert include == expected
    # m3_call is what makes a narrow filter lossless; tools_load_domain is what
    # keeps lazy mode from being a dead end. Both must be present.
    assert "m3_call" in include
    assert "tools_load_domain" in include


# ── the version gate: it must trip, and it must stay quiet ───────────────────

def test_gate_trips_below_the_floor_and_writes_nothing(wired, capsys):
    """Planted violation. A gate that warns but still writes is blind."""
    calls = wired(version="OpenClaw 2026.3.11 (deadbee)")
    assert sw._wire_openclaw() is False

    assert not [c for c in calls if "set" in c], "refused but still called `mcp set`"
    out = capsys.readouterr().out
    assert "2026.3.22" in out
    assert "npm install -g openclaw@latest" in out
    # §3 evidence levels: state what was OBSERVED, not an inferred cause.
    assert "observed:" in out
    assert "2026.3.11" in out


def test_gate_is_silent_on_a_healthy_version(wired, capsys):
    """A warning on a healthy system is the §3 false alarm, not a safe default."""
    wired(version=_REAL_BANNER)
    assert sw._wire_openclaw() is True
    out = capsys.readouterr().out
    assert "[!]" not in out, f"warned on a healthy install: {out}"


def test_exact_floor_version_is_accepted(wired):
    """Guards a `>` vs `>=` slip at the boundary."""
    calls = wired(version="OpenClaw 2026.3.22 (abc1234)")
    assert sw._wire_openclaw() is True
    assert [c for c in calls if "set" in c]


@pytest.mark.parametrize("banner", [
    _REAL_BANNER,                                   # the real shape
    "OpenClaw 2026.4.1-beta.1 (deadbee)",           # prerelease suffix
    "[warn] config anomaly at line 12\nOpenClaw 2026.3.28 (f9b1079)\nWhy did the claw cross the road?",
    "",                                             # empty
    "OpenClaw (dev build)",                         # no digits at all
    "totally unexpected output",
])
def test_unparseable_or_noisy_version_never_raises(wired, banner):
    """_parse_version RAISES TypeError on these; the regex must run first.

    Measured 2026-09-18: rust_core_install._parse_version('OpenClaw 2026.3.28
    (f9b1079)') and ('') both raise TypeError, because it mixes ints and tuples.
    Extracting the dotted run first — and keeping the compare in try/except — is
    what makes these safe. A raise here would abort `m3 setup`.
    """
    calls = wired(version=banner)
    assert sw._wire_openclaw() is True          # unknown version proceeds
    assert [c for c in calls if "set" in c]


# ── failure paths ────────────────────────────────────────────────────────────

def test_missing_cli_warns_and_runs_nothing(wired, capsys):
    calls = wired(exe=None)
    assert sw._wire_openclaw() is False
    assert calls == [], "probed the CLI despite which() returning None"
    assert "npm install -g openclaw" in capsys.readouterr().out


def test_failure_echoes_the_exact_argv(wired, capsys):
    """The split-brain guard: never hand the user a SIMPLIFIED command.

    A shortened form drops the root-pinning env, so a user who follows it gets a
    server on DEFAULT roots while their chatlog hook writes the pinned ones.
    """
    wired(set_rc=1, set_out="boom")
    assert sw._wire_openclaw() is False
    out = capsys.readouterr().out
    assert "M3_ENGINE_ROOT" in out, "echoed argv lost the root pins"
    assert "mcp" in out and "set" in out


def test_echoed_argv_carries_no_secrets(wired, capsys):
    """Printing the argv is only safe while the env is secret-free.

    _canonical_memory_env() returns M3_*_ROOT paths today. If anyone adds a token
    to it, the failure echo becomes a credential leak — this is the enforcement
    site for that (§3: a limit with no enforcement site is a lie).
    """
    import m3_memory.installer as inst

    real = inst._canonical_memory_env
    try:
        keys = set(real().keys())
    except Exception:  # pragma: no cover — env resolution is environment-specific
        pytest.skip("_canonical_memory_env unavailable in this environment")
    leaky = {k for k in keys if any(t in k.upper() for t in ("TOKEN", "SECRET", "KEY", "PASS"))}
    assert not leaky, f"canonical env now carries secret-shaped keys: {sorted(leaky)}"

    wired(set_rc=1)
    sw._wire_openclaw()
    out = capsys.readouterr().out
    for t in ("TOKEN", "SECRET", "PASSWORD"):
        assert t not in out.upper(), f"{t} appeared in the echoed argv"


def test_ensure_payload_importable_actually_adds_bin(monkeypatch):
    """_ensure_payload_importable() must PUT bin/ on sys.path, not assume it.

    The defect this pins: _canonical_memory_server() reaches m3_sdk, bin/ is not
    a package, so `m3 setup --agents openclaw` died with
    ModuleNotFoundError('m3_sdk') while 104 mocked tests stayed green.

    ⚠ The obvious test — "call the helper, then import m3_sdk" — is BLIND here,
    verified by planting: line 30 of this file already puts bin/ on sys.path at
    import time, so the import succeeds even with the helper gutted. That is the
    §3 hermeticity trap (a test that passes because something else provides the
    dependency). So this strips bin/ from sys.path first and asserts the helper
    restores it.
    """
    bd = str(sw._bin_dir())
    stripped = [p for p in sys.path if os.path.abspath(p) != os.path.abspath(bd)]
    monkeypatch.setattr(sys, "path", stripped)
    assert not any(os.path.abspath(p) == os.path.abspath(bd) for p in sys.path)

    sw._ensure_payload_importable()

    assert any(os.path.abspath(p) == os.path.abspath(bd) for p in sys.path), (
        "_ensure_payload_importable() did not add the payload bin/ to sys.path — "
        "_canonical_memory_server() will raise ModuleNotFoundError('m3_sdk')."
    )


def test_wire_openclaw_builds_a_real_spec_with_no_env_mock(monkeypatch):
    """End-to-end-ish: the UNMOCKED root resolution must produce a full spec.

    Only subprocess and which() are faked; _canonical_memory_env is NOT patched,
    so this exercises the path that actually broke. Asserts on the JSON argv the
    code would hand OpenClaw.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(sw.shutil, "which", lambda name: _FAKE_EXE)

    def fake_run(cmd, *a, **k):
        calls.append(list(cmd))
        if "--version" in cmd:
            return SimpleNamespace(returncode=0, stdout=_REAL_BANNER, stderr="")
        return SimpleNamespace(returncode=0, stdout="saved", stderr="")

    monkeypatch.setattr(sw.subprocess, "run", fake_run)
    assert sw._wire_openclaw() is True

    spec = _payload(calls)
    assert spec["command"], "real spec has no command"
    assert spec["transport"] == "stdio"
    for root in ("M3_ENGINE_ROOT", "M3_CONFIG_ROOT"):
        assert root in spec["env"], f"real spec lost {root}: {spec['env']}"
    # 10 tools, derived — not the 115-tool fallback that a failed import causes.
    assert len(spec["toolFilter"]["include"]) == 10


def test_spec_is_complete_because_mcp_set_overwrites(wired):
    """`openclaw mcp set` REPLACES an entry wholesale — verified on a live CLI.

    A second write omitting `env` dropped the env from the first. So no
    remove-first step is needed (unlike claude mcp add), but every write must
    carry the full spec. This pins that the spec has all four required parts.
    """
    calls = wired()
    assert sw._wire_openclaw() is True
    spec = _payload(calls)
    for k in ("command", "env", "transport", "enabled"):
        assert k in spec, f"incomplete spec would silently drop {k}: {spec}"
