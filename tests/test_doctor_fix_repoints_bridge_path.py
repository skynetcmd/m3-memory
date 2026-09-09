"""`m3 doctor --fix` must repair the config key its own warning names.

WHY THIS EXISTS
---------------
memory_bridge.py's canonical-path guard warns on EVERY start when the running
bridge disagrees with the installer config's ``bridge_path``:

    this bridge (...) differs from the recorded install (...) — the launching
    agent config may be stale. Run `m3 doctor --fix` to repoint it.

`doctor --fix` did not touch that key. It repoints *agent MCP configs*, a
different thing, then prints "agent MCP configs: 1 repointed." and
"Repair Summary: OK" — so the user reasonably believes it is fixed, and the
warning fires again on the next start. Measured 2026-09-09 on an install whose
config still named a legacy ``~/.m3-memory/repo/bin`` path months after the
payload moved into the pipx venv.

A remedy that reports success without remedying is worse than none: it spends
the trust that makes the warning worth printing (§3 — a false signal trains you
to ignore the real one).

The dev-checkout case is the subtle half. ``find_bridge()`` also honours
``$M3_PATH_BIN`` and a dev-checkout sibling; recording either as "the install"
would persist a developer's working tree (or a one-off env override) into the
config. Those divergences are legitimate, so the repair must stay quiet there —
tested below, because a fix that over-reaches here is a worse bug than the one
it replaces.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from m3_memory import installer  # noqa: E402


@pytest.fixture()
def cfg_home(tmp_path, monkeypatch):
    """Point the installer's config dir at a temp tree."""
    monkeypatch.setattr(installer, "config_dir", lambda: tmp_path)
    return tmp_path


def _fake_packaged(monkeypatch, tmp_path):
    """Stand in for the wheel-packaged bridge, so these tests run in a CHECKOUT.

    The repair keys off ``Path(installer.__file__).parent / "bin" /
    memory_bridge.py`` — which does not exist in a source checkout, so guarding
    on ``packaged.is_file()`` made every meaningful assertion SKIP here and in
    CI. A gate that skips is not a gate. Point ``installer.__file__`` at a temp
    package dir holding a real file instead.
    """
    pkg = tmp_path / "pkg"
    (pkg / "bin").mkdir(parents=True)
    bridge = pkg / "bin" / "memory_bridge.py"
    bridge.write_text("# packaged", encoding="utf-8")
    monkeypatch.setattr(installer, "__file__", str(pkg / "installer.py"))
    return bridge.resolve()


def _write_cfg(tmp_path, bridge_path):
    (tmp_path / "config.json").write_text(
        json.dumps({"bridge_path": str(bridge_path), "version": "test"}),
        encoding="utf-8",
    )


def test_fix_repoints_a_stale_bridge_path(cfg_home, monkeypatch, tmp_path):
    """The bug: a stale key stayed stale through --fix."""
    packaged = _fake_packaged(monkeypatch, tmp_path)

    _write_cfg(cfg_home, r"C:\legacy\.m3-memory\repo\bin\memory_bridge.py")
    monkeypatch.setattr(installer, "find_bridge", lambda: packaged)

    out = installer._heal_config_bridge_path(apply=True)

    assert out and out[0].startswith("[+]"), f"expected a repair line, got {out!r}"
    written = json.loads((cfg_home / "config.json").read_text(encoding="utf-8"))
    assert installer.Path(written["bridge_path"]).resolve() == packaged
    assert written["version"] == "test", "unrelated config keys must survive"


def test_fix_is_idempotent_when_already_correct(cfg_home, monkeypatch, tmp_path):
    packaged = _fake_packaged(monkeypatch, tmp_path)

    _write_cfg(cfg_home, packaged)
    monkeypatch.setattr(installer, "find_bridge", lambda: packaged)

    assert installer._heal_config_bridge_path(apply=True) == [], (
        "a correct config must produce no repair line — otherwise every run "
        "reports work it did not do"
    )


def test_readonly_doctor_reports_but_does_not_write(cfg_home, monkeypatch, tmp_path):
    """`doctor` (no --fix) must SAY the key is stale and change nothing."""
    packaged = _fake_packaged(monkeypatch, tmp_path)

    stale = r"C:\legacy\.m3-memory\repo\bin\memory_bridge.py"
    _write_cfg(cfg_home, stale)
    monkeypatch.setattr(installer, "find_bridge", lambda: packaged)

    out = installer._heal_config_bridge_path(apply=False)

    assert out and out[0].startswith("[!]"), f"expected a report line, got {out!r}"
    unchanged = json.loads((cfg_home / "config.json").read_text(encoding="utf-8"))
    assert unchanged["bridge_path"] == stale, "read-only doctor must not write"


def test_a_dev_checkout_bridge_is_never_recorded(cfg_home, monkeypatch, tmp_path):
    """The guard: never persist a dev tree / $M3_PATH_BIN as 'the install'.

    find_bridge() honours both, and they are legitimate, deliberate
    divergences. Writing one into the config turns a transient path into
    permanent state — a worse bug than the stale key this repair fixes.
    """
    dev = tmp_path / "devcheckout" / "bin" / "memory_bridge.py"
    dev.parent.mkdir(parents=True)
    dev.write_text("# dev checkout", encoding="utf-8")

    stale = r"C:\legacy\.m3-memory\repo\bin\memory_bridge.py"
    _write_cfg(cfg_home, stale)
    monkeypatch.setattr(installer, "find_bridge", lambda: dev)

    assert installer._heal_config_bridge_path(apply=True) == [], (
        "a dev-checkout/env-override bridge must NOT be recorded as the install"
    )
    unchanged = json.loads((cfg_home / "config.json").read_text(encoding="utf-8"))
    assert unchanged["bridge_path"] == stale
