"""CI guard: m3's published plugins must pass their HOST's OWN validator.

Rather than checking plugin manifests against a hand-maintained schema (or against
how some peer plugin happens to be structured), run each host's authoritative
validator against m3's plugin dir:

  * Claude Code:  `claude plugin validate .`      (validates .claude-plugin/)
  * Antigravity:  `agy plugin validate .antigravity-plugin`

Both are skipped when the binary isn't on PATH (CI images may lack them), so this
is a guard that fires wherever the tooling exists, not a hard dependency.

IMPORTANT — exit codes are UNRELIABLE: both `claude plugin validate` and
`agy plugin validate` print "Validation failed" / "Error:" but still exit 0
(verified 2026-07-21). So we assert on OUTPUT MARKERS, never the return code —
a naive `returncode == 0` check would pass a broken manifest.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], cwd: Path) -> str:
    """Run a validator and return combined stdout+stderr (exit code ignored)."""
    p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=60)
    return (p.stdout or "") + (p.stderr or "")


@pytest.mark.skipif(shutil.which("claude") is None,
                    reason="claude binary not on PATH — plugin validator unavailable")
def test_claude_plugin_validates():
    """`claude plugin validate .` must report success for m3's .claude-plugin.

    Fails on the FAILURE marker, not the (unreliable) exit code."""
    out = _run(["claude", "plugin", "validate", "."], _ROOT)
    assert "Validation failed" not in out, f"claude plugin validate failed:\n{out}"
    assert "Validation passed" in out, (
        f"claude plugin validate did not confirm success:\n{out}")


@pytest.mark.skipif(shutil.which("agy") is None,
                    reason="agy binary not on PATH — Antigravity validator unavailable")
def test_antigravity_plugin_validates():
    """`agy plugin validate .antigravity-plugin` must report [ok] with no error."""
    plugin_dir = _ROOT / ".antigravity-plugin"
    assert plugin_dir.is_dir(), f"missing {plugin_dir}"
    out = _run(["agy", "plugin", "validate", str(plugin_dir)], _ROOT)
    assert "[error]" not in out and "Error:" not in out, (
        f"agy plugin validate reported an error:\n{out}")
    assert "[ok]" in out, f"agy plugin validate did not confirm [ok]:\n{out}"


@pytest.mark.skipif(shutil.which("claude") is None,
                    reason="claude binary not on PATH")
def test_claude_marketplace_validates():
    """The marketplace manifest itself must validate (users add it via
    `/plugin marketplace add skynetcmd/m3-memory`)."""
    out = _run(["claude", "plugin", "validate",
                str(_ROOT / ".claude-plugin" / "marketplace.json")], _ROOT)
    assert "Validation failed" not in out, f"marketplace validate failed:\n{out}"
    assert "Validation passed" in out, f"marketplace did not validate:\n{out}"
