"""Doctor must flag m3 commands wired at a STALE payload clone, not just dead paths.

Regression for the ~/.m3/repo drift: after `pip install -U`, the core memory
bridge follows the wheel while aux bridges + capture hooks keep pointing at an
old ~/.m3/repo clone. The files still EXIST (so the dead-path check passes) yet
run old code. `_scan_stale_payload` catches "exists but not under the current
payload bin_dir()".
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from doctor import agent_paths_probe as p  # noqa: E402


def _write_settings(home, payload_bin, clone_bin):
    claude = home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    # Create the referenced script files so this is purely a "wrong root", not a
    # "dead path", test — both bins exist on disk.
    for base in (payload_bin, clone_bin):
        (base / "hooks" / "chatlog").mkdir(parents=True, exist_ok=True)
        for f in ("memory_bridge.py", "custom_tool_bridge.py", "statusline-command.sh"):
            (base / f).write_text("# stub")
        (base / "hooks" / "chatlog" / "claude_code_precompact.sh").write_text("# stub")

    settings = {
        "mcpServers": {
            # core bridge → wheel payload (correct)
            "memory": {"command": "/py", "args": [str(payload_bin / "memory_bridge.py")]},
            # aux bridge → stale clone (wrong)
            "custom_pc_tool": {"command": "/py", "args": [str(clone_bin / "custom_tool_bridge.py")]},
            # a user's own server must never be flagged
            "user_thing": {"command": "/py", "args": [str(clone_bin / "not_ours.py")]},
        },
        "hooks": {
            "Stop": [{"hooks": [{"type": "command",
                                 "command": f"/py {clone_bin / 'hooks' / 'chatlog' / 'claude_code_precompact.sh'}"}]}],
        },
        "statusLine": {"type": "command",
                       "command": f"/py {clone_bin / 'statusline-command.sh'}"},
    }
    (claude / "settings.json").write_text(json.dumps(settings))


def test_flags_stale_clone_paths_but_not_wheel_or_user(monkeypatch, tmp_path):
    payload_bin = tmp_path / "wheel" / "m3_memory" / "bin"
    clone_bin = tmp_path / ".m3" / "repo" / "bin"
    payload_bin.mkdir(parents=True)
    clone_bin.mkdir(parents=True)
    _write_settings(tmp_path, payload_bin, clone_bin)

    monkeypatch.setattr(p, "_current_payload_bin", lambda: str(payload_bin))
    monkeypatch.setattr(p.Path, "home", classmethod(lambda cls: tmp_path))

    stale = dict(p._scan_stale_payload())
    labels = set(stale)

    # aux bridge + hook + statusline under the clone are flagged
    assert "Claude/custom_pc_tool" in labels
    assert "Claude/hook:Stop" in labels
    assert "Claude/statusLine" in labels
    # the wheel-rooted core bridge is NOT flagged
    assert "Claude/memory" not in labels
    # a non-m3 server is never judged
    assert "Claude/user_thing" not in labels


def test_no_flags_when_everything_under_payload(monkeypatch, tmp_path):
    payload_bin = tmp_path / "wheel" / "m3_memory" / "bin"
    payload_bin.mkdir(parents=True)
    _write_settings(tmp_path, payload_bin, payload_bin)  # everything under the payload

    monkeypatch.setattr(p, "_current_payload_bin", lambda: str(payload_bin))
    monkeypatch.setattr(p.Path, "home", classmethod(lambda cls: tmp_path))

    assert p._scan_stale_payload() == []
