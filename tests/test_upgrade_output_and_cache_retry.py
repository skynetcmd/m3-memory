"""Upgrade friction seen in the 2026.10.7.0 deploys.

- A plugin update made step 5 print the whole doctor report: the update lines
  were not counted as healthy. The fixture below is that report, verbatim.
- The summary told the user to `/mcp` while the plugin step said to restart
  Claude Code; a new plugin loads only on restart.
- pip's cached index hid a release published minutes earlier (SkyPC, twice).
- Every upgrade repeated "no local LLM runtime detected" on a host that never
  had one.
"""
from __future__ import annotations

import importlib.util
import pathlib

from m3_memory import setup_wizard

_BIN = pathlib.Path(__file__).resolve().parent.parent / "bin"
_SPEC = importlib.util.spec_from_file_location("m3_upgrade_friction", _BIN / "m3_upgrade.py")
assert _SPEC and _SPEC.loader
m3u = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m3u)

# Step 5 of the Mac's 2026.10.6.0 -> 2026.10.7.0 upgrade, verbatim.
_DOCTOR_WITH_PLUGIN_UPDATE = """\
[OK] m3 HEALTHY · 82019 memories · embedder: shared server (:8082) · chatlog: active (144535 rows)

agent MCP configs: all healthy.
[OK] memory bridge found
==> Running m3-memory self-repair...
Repair Summary: NOTHING_TO_DO (run_migrations skipped, rebuild_fts5 skipped, embed_backfill skipped, rebuild_cohesion skipped)
✅ shared-embedder: OK (config + server + keep-alive)
[OK] Web Dashboard available at: http://127.0.0.1:8088  (pid 25578)
✅ agent paths: OK (1 wired host(s), no dead or stale paths)
✅ claude mcp: single direct server (mcp__m3_memory__)
  [ok] plugin plugin: ✔ Plugin "m3" updated from 2026.10.6.0 to 2026.10.7.0 for scope user. Restart to apply changes.
  [ok] plugin restart: restart Claude Code to load the new version
"""


def test_a_plugin_update_still_collapses_to_the_health_line():
    assert m3u.doctor_is_all_healthy(_DOCTOR_WITH_PLUGIN_UPDATE)
    assert m3u.plugin_update(_DOCTOR_WITH_PLUGIN_UPDATE) == ("2026.10.6.0", "2026.10.7.0")


def test_a_real_problem_still_prints_the_report():
    bad = _DOCTOR_WITH_PLUGIN_UPDATE + "⚠️  cognitive loop: 0 entities\n"
    assert not m3u.doctor_is_all_healthy(bad)


def test_no_plugin_line_means_no_plugin_update():
    assert m3u.plugin_update("[OK] m3 HEALTHY · 1 memories\n") is None


def _summary(**kw):
    base = dict(old="2026.10.6.0", new="2026.10.7.0", unchanged=False,
                agents_stopped=0, failed_step="", rc=0, log="")
    return "\n".join(m3u.summary_lines(**{**base, **kw}))


def test_after_a_plugin_update_the_summary_says_restart_claude_code():
    out = _summary(plugin_updated=True)
    assert "restart Claude Code (its m3 plugin was updated)" in out
    assert "(Claude Code: /mcp)" not in out


def test_without_a_plugin_update_reconnecting_is_enough():
    out = _summary()
    assert "reconnect m3 (Claude Code: /mcp)" in out


def test_the_uncached_retry_fires_only_when_pypi_has_another_version():
    retry = m3u.should_retry_uncached
    assert retry(unchanged=True, local_source=False, latest="2026.10.7.0", installed="2026.10.6.0")
    # Already current: no second install.
    assert not retry(unchanged=True, local_source=False, latest="2026.10.7.0",
                     installed="2026.10.7.0")
    # PyPI unreachable: do not guess.
    assert not retry(unchanged=True, local_source=False, latest=None, installed="2026.10.6.0")
    # A local wheel or path source: the index is not involved.
    assert not retry(unchanged=True, local_source=True, latest="2026.10.7.0",
                     installed="2026.10.6.0")
    # It changed: nothing to retry.
    assert not retry(unchanged=False, local_source=False, latest="2026.10.7.0",
                     installed="2026.10.6.0")


def test_the_retry_bypasses_both_caches():
    env = m3u.no_cache_env()
    assert env["PIP_NO_CACHE_DIR"] == "1" and env["UV_NO_CACHE"] == "1"


def _no_llm(monkeypatch, enabled_vars=()):
    monkeypatch.delenv("M3_LLM_URL", raising=False)
    monkeypatch.delenv("M3_LLM_ENDPOINTS_CSV", raising=False)
    monkeypatch.delenv("LLM_ENDPOINTS_CSV", raising=False)
    monkeypatch.setattr(setup_wizard, "_endpoint_reachable", lambda *_a, **_k: False)
    monkeypatch.setattr(setup_wizard, "_called_by_upgrade", lambda: True)
    monkeypatch.setattr(setup_wizard, "_llm_switch_enabled", lambda var: var in enabled_vars)


def test_upgrade_says_nothing_when_no_llm_runtime_was_ever_set_up(monkeypatch, capsys):
    _no_llm(monkeypatch)
    setup_wizard._probe_llm_endpoints(None, None)
    assert capsys.readouterr().out == ""


def test_upgrade_warns_when_an_enabled_runtime_went_away(monkeypatch, capsys):
    _no_llm(monkeypatch, enabled_vars=("M3_ENABLE_OLLAMA_FAILOVER",))
    setup_wizard._probe_llm_endpoints(None, None)
    out = capsys.readouterr().out
    assert "Ollama enabled for enrichment but not reachable now" in out
