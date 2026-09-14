"""The startup-tool-set resolver: precedence, refusals, and the cycle ban.

What this pins:

  1. PRECEDENCE, all four levels, including which one wins when several apply.
  2. REFUSALS are loud. An unknown tool name, an empty set, or a set missing the
     escape hatch must raise -- not warn and continue with something else. A
     session quietly missing its most-used tool is the failure this resolver
     exists to prevent (section 3).
  3. The default is DERIVED, never written to disk. Materializing it at install
     would freeze the user's startup set against future upgrades.
  4. NO IMPORT CYCLE. memory_bridge imports this at startup, so this module must
     not reach back into mcp_tool_catalog / memory_core / memory_bridge.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
if str(_BIN) not in sys.path:
    sys.path.insert(0, str(_BIN))

import tools_config as tc  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    """Every test starts from a known state: no env overrides, empty config root."""
    monkeypatch.delenv(tc.ENV_STARTUP, raising=False)
    monkeypatch.delenv(tc.ENV_LAZY, raising=False)
    monkeypatch.setenv("M3_CONFIG_ROOT", str(tmp_path))
    return tmp_path


def _write(tmp_path, payload):
    p = tmp_path / tc.CONFIG_FILENAME
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


# ── precedence ───────────────────────────────────────────────────────────────
def test_default_when_nothing_is_set():
    names, source = tc.resolve_startup_tools()
    assert names == list(tc.DEFAULT_STARTUP_TOOLS)
    assert source == "shipped default"


def test_config_file_beats_the_default(_clean_env):
    _write(_clean_env, {"startup_tools": ["memory_search", *tc.ESCAPE_HATCH]})
    names, source = tc.resolve_startup_tools()
    assert names == ["memory_search", *tc.ESCAPE_HATCH]
    assert tc.CONFIG_FILENAME in source


def test_env_beats_the_config_file(_clean_env, monkeypatch):
    _write(_clean_env, {"startup_tools": ["memory_search", *tc.ESCAPE_HATCH]})
    monkeypatch.setenv(tc.ENV_STARTUP, "chatlog_search," + ",".join(tc.ESCAPE_HATCH))
    names, source = tc.resolve_startup_tools()
    assert names[0] == "chatlog_search"
    assert source.startswith(tc.ENV_STARTUP)


def test_lazy_off_beats_everything(_clean_env, monkeypatch):
    """A global off-switch must not be narrowed by a stale startup_tools list."""
    _write(_clean_env, {"startup_tools": ["memory_search", *tc.ESCAPE_HATCH]})
    monkeypatch.setenv(tc.ENV_STARTUP, "chatlog_search")
    monkeypatch.setenv(tc.ENV_LAZY, "0")
    names, source = tc.resolve_startup_tools()
    assert names is None, "None means 'register everything', distinct from an empty list"
    assert tc.ENV_LAZY in source


def test_none_is_not_an_empty_list():
    """The two mean opposite things and must never be conflated."""
    assert tc.DEFAULT_STARTUP_TOOLS
    assert [] != None  # noqa: E711 — the point is that a caller must check `is None`


# ── the escape hatch is non-removable ────────────────────────────────────────
def test_config_omitting_the_escape_hatch_is_refused(_clean_env):
    _write(_clean_env, {"startup_tools": ["memory_search"]})
    with pytest.raises(tc.ToolsConfigError, match="escape hatch"):
        tc.resolve_startup_tools()


def test_refusal_names_every_missing_hatch_tool(_clean_env):
    _write(_clean_env, {"startup_tools": ["memory_search", "m3_call"]})
    with pytest.raises(tc.ToolsConfigError) as e:
        tc.resolve_startup_tools()
    assert "tools_list_domains" in str(e.value)
    assert "tools_load_domain" in str(e.value)


def test_refuses_rather_than_silently_forcing_them_in(_clean_env):
    """Forcing the hatch in would be friendlier; refusing is honest. A silently
    amended config means the file no longer describes the running system."""
    _write(_clean_env, {"startup_tools": ["memory_search"]})
    with pytest.raises(tc.ToolsConfigError):
        tc.resolve_startup_tools()


def test_env_omitting_the_escape_hatch_is_also_refused(monkeypatch):
    monkeypatch.setenv(tc.ENV_STARTUP, "memory_search")
    with pytest.raises(tc.ToolsConfigError, match="escape hatch"):
        tc.resolve_startup_tools()


# ── empty / malformed ────────────────────────────────────────────────────────
def test_empty_startup_tools_is_refused(_clean_env):
    _write(_clean_env, {"startup_tools": []})
    with pytest.raises(tc.ToolsConfigError, match="empty"):
        tc.resolve_startup_tools()


def test_non_list_startup_tools_is_refused(_clean_env):
    _write(_clean_env, {"startup_tools": "memory_search"})
    with pytest.raises(tc.ToolsConfigError, match="list of tool names"):
        tc.resolve_startup_tools()


def test_malformed_json_warns_and_falls_back(_clean_env, caplog):
    """Softer than an unknown name on purpose: a truncated file is an accident
    with an obvious fix, and refusing to start m3 over it is worse."""
    (_clean_env / tc.CONFIG_FILENAME).write_text("{not json", encoding="utf-8")
    names, source = tc.resolve_startup_tools()
    assert names == list(tc.DEFAULT_STARTUP_TOOLS)
    assert source == "shipped default"


def test_duplicates_are_deduped_in_order(monkeypatch):
    monkeypatch.setenv(tc.ENV_STARTUP,
                       "memory_search,memory_search," + ",".join(tc.ESCAPE_HATCH))
    names, _ = tc.resolve_startup_tools()
    assert names.count("memory_search") == 1
    assert names[0] == "memory_search"


# ── unknown names fail loud, with suggestions ────────────────────────────────
def test_unknown_tool_name_raises():
    with pytest.raises(tc.ToolsConfigError, match="Unknown tool name"):
        tc.validate_against_catalog(["no_such_tool"], {"memory_search", "m3_call"})


def test_unknown_name_gets_a_did_you_mean():
    """A tool renamed by an upgrade must say so, not vanish silently."""
    with pytest.raises(tc.ToolsConfigError) as e:
        tc.validate_against_catalog(["memory_search_slm"],
                                    {"memory_search_slim", "m3_call"})
    assert "did you mean" in str(e.value)
    assert "memory_search_slim" in str(e.value)


def test_known_names_validate_silently():
    tc.validate_against_catalog(["memory_search"], {"memory_search", "m3_call"})


def test_the_shipped_default_is_valid_against_the_real_catalog():
    """The strongest check here: the default set this version ships must
    actually exist in this version's catalog. Importing the catalog in the TEST
    is fine -- it is the resolver that must not."""
    import mcp_tool_catalog as cat
    tc.validate_against_catalog(list(tc.DEFAULT_STARTUP_TOOLS),
                                {t.name for t in cat.TOOLS})


# ── the default is never materialized ────────────────────────────────────────
def test_resolving_does_not_create_a_config_file(_clean_env):
    """Writing the default at install would freeze the user's startup set
    against every future upgrade."""
    tc.resolve_startup_tools()
    assert not (_clean_env / tc.CONFIG_FILENAME).exists()


def test_starter_config_is_written_only_on_request(_clean_env):
    path = tc.write_starter_config()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["startup_tools"] == list(tc.DEFAULT_STARTUP_TOOLS)
    assert "overrides" in data["_comment"].lower(), (
        "a user opting into a file must be told it pins them against upgrades"
    )


def test_starter_config_round_trips(_clean_env):
    tc.write_starter_config()
    names, source = tc.resolve_startup_tools()
    assert names == list(tc.DEFAULT_STARTUP_TOOLS)
    assert tc.CONFIG_FILENAME in source


# ── no import cycle (section 12b / the reason this module is stdlib-only) ────
def test_resolver_does_not_import_the_catalog():
    """memory_bridge imports this at startup; an import back would mean the
    catalog has to be built before the set that decides what to build."""
    src = (_BIN / "tools_config.py").read_text(encoding="utf-8")
    for banned in ("import mcp_tool_catalog", "import memory_core",
                   "import memory_bridge", "from mcp_tool_catalog",
                   "from memory_core", "from memory_bridge"):
        assert banned not in src, f"tools_config must not {banned!r} (cycle)"


def test_resolver_imports_without_the_catalog_on_the_path(tmp_path):
    """Stronger than reading the source: import it in a subprocess whose path
    cannot see the catalog, and confirm it still resolves."""
    import subprocess
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "import tools_config as tc;"
        "n,s = tc.resolve_startup_tools();"
        "print(len(n), s)"
    ) % str(_BIN)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "shipped default" in r.stdout


# ── the doctor probe (section 3: observable, not guessed) ────────────────────
def test_probe_reports_the_set_and_its_source(capsys):
    from doctor import startup_tools_probe
    assert startup_tools_probe.run() == 0
    out = capsys.readouterr().out
    assert "shipped default" in out, "the probe must name the precedence level"
    for name in tc.DEFAULT_STARTUP_TOOLS:
        assert name in out, f"{name} missing from the probe's report"
    assert "escape hatch" in out


def test_probe_fails_when_the_escape_hatch_is_missing(monkeypatch, capsys):
    """Not a degraded-but-working state: m3 would start with a surface the user
    did not ask for, and from inside a session that looks like the tool simply
    not existing."""
    from doctor import startup_tools_probe
    monkeypatch.setenv(tc.ENV_STARTUP, "memory_search")
    assert startup_tools_probe.run() == 1
    assert "escape hatch" in capsys.readouterr().out


def test_probe_fails_on_an_unknown_tool_name(monkeypatch, capsys):
    from doctor import startup_tools_probe
    monkeypatch.setenv(tc.ENV_STARTUP,
                       "memory_search_slm," + ",".join(tc.ESCAPE_HATCH))
    assert startup_tools_probe.run() == 1
    out = capsys.readouterr().out
    assert "did you mean" in out and "memory_search_slim" in out


def test_probe_handles_lazy_disabled(monkeypatch, capsys):
    from doctor import startup_tools_probe
    monkeypatch.setenv(tc.ENV_LAZY, "0")
    assert startup_tools_probe.run() == 0
    assert "ALL tools" in capsys.readouterr().out
