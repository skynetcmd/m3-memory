"""Setup asks for Windows administrator rights once, not once per step.

Hazard: every privileged step (boot-task registration, legacy task removal)
raised its own UAC dialog, and a dialog lost behind the terminal silently
dropped its step. These tests never raise a real prompt: the elevated process
is replaced by a stub that writes what the real one would.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

from m3_memory import elevate, setup_wizard


def test_the_script_runs_each_action_once_and_quotes_safely():
    b = elevate.ElevationBatch()
    b.add("repair", ["C:/Py/python.exe", "C:/it's here/install_schedules.py", "--repair"])
    b.add("repair again", ["C:/Py/python.exe", "C:/it's here/install_schedules.py", "--repair"])
    b.add("delete", ["schtasks", "/Delete", "/TN", "AgentOS_Maintenance", "/F"])
    assert len(b) == 2, "the same command queued twice runs once"
    script = b.render_script("C:/t/results.json", "C:/t")
    assert "'C:/it''s here/install_schedules.py'" in script
    assert script.count("$LASTEXITCODE") == 2
    assert "ConvertTo-Json" in script


def _fake_elevated(monkeypatch, codes: dict, outputs: dict):
    """Stand in for `Start-Process -Verb RunAs`: write results + logs."""
    def fake_run(argv, **_kw):
        launcher = argv[-1]
        script = launcher.split("'-File',")[1].split(" -Verb")[0].strip().strip("'")
        tmp = os.path.dirname(script)
        with open(os.path.join(tmp, "results.json"), "w", encoding="utf-8") as fh:
            json.dump(codes, fh)
        for i, text in outputs.items():
            with open(os.path.join(tmp, f"{i}.log"), "w", encoding="utf-16") as fh:
                fh.write(text)
        return type("R", (), {"returncode": 0})()
    monkeypatch.setattr(elevate.subprocess, "run", fake_run)
    monkeypatch.setattr(elevate.sys, "platform", "win32")


def test_run_reports_each_step_with_its_output(monkeypatch):
    b = elevate.ElevationBatch()
    b.add("repair", ["py", "s.py", "--repair"])
    b.add("delete", ["schtasks", "/Delete", "/TN", "X", "/F"])
    _fake_elevated(monkeypatch, {"repair": 0, "delete": 1}, {1: "ERROR: not found\n"})
    assert b.run() == {"repair": {"rc": 0, "output": ""},
                       "delete": {"rc": 1, "output": "ERROR: not found"}}


def test_a_declined_prompt_means_nothing_ran(monkeypatch):
    b = elevate.ElevationBatch()
    b.add("repair", ["py", "s.py", "--repair"])
    monkeypatch.setattr(elevate.sys, "platform", "win32")
    monkeypatch.setattr(elevate.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 1})())  # no results file
    assert b.run() is None


@pytest.fixture
def batching(monkeypatch):
    monkeypatch.setattr(setup_wizard.sys, "platform", "win32")
    monkeypatch.setattr(setup_wizard, "_stdin_is_interactive", lambda: True)
    monkeypatch.setattr(setup_wizard, "_ELEVATION", None)
    setup_wizard._begin_elevation_batch()
    yield
    monkeypatch.setattr(setup_wizard, "_ELEVATION", None)


def test_setup_queues_privileged_steps_and_asks_once(batching, monkeypatch, capsys):
    asked, runs = [], []
    monkeypatch.setattr(setup_wizard, "_ask_yes_no", lambda q, default=True: asked.append(q) or True)
    monkeypatch.setattr(setup_wizard, "_runas_schedule_repair_windows",
                        lambda s: pytest.fail("must queue, not prompt now"))

    assert setup_wizard._offer_elevated_schedule_repair("s.py", non_interactive=True) is None
    assert setup_wizard._offer_elevated_schedule_repair("s.py", non_interactive=True) is None
    assert setup_wizard._offer_elevated_task_delete(["AgentOS_Maintenance"],
                                                    non_interactive=True) is None
    assert asked == [], "queuing asks nothing"

    def fake_run(self):
        runs.append([label for label, _ in self.actions])
        return {label: {"rc": 0 if "boot" in label else 1, "output": "denied"}
                for label, _ in self.actions}
    monkeypatch.setattr(elevate.ElevationBatch, "run", fake_run)

    setup_wizard._flush_elevation()
    out = capsys.readouterr().out
    assert len(runs) == 1 and len(runs[0]) == 2, "one elevated run, duplicates merged"
    assert len(asked) == 1 and "one Windows admin prompt" in asked[0]
    assert "boot-start services" in out and "done" in out
    assert "remove legacy scheduled task AgentOS_Maintenance: failed (exit 1)" in out


def test_an_early_stop_names_the_steps_that_did_not_run(batching, capsys):
    setup_wizard._offer_elevated_schedule_repair("s.py", non_interactive=True)
    setup_wizard._report_unrun_elevation()
    out = capsys.readouterr().out
    assert "did not run because setup stopped early" in out
    assert "boot-start services" in out


def test_without_a_human_nothing_is_queued(monkeypatch):
    monkeypatch.setattr(setup_wizard.sys, "platform", "win32")
    monkeypatch.setattr(setup_wizard, "_stdin_is_interactive", lambda: False)
    monkeypatch.setattr(setup_wizard, "_ELEVATION", None)
    setup_wizard._begin_elevation_batch()
    assert setup_wizard._ELEVATION is None
    assert sys.platform  # keep the import used


def test_a_stale_windows_embed_service_restart_joins_the_one_prompt(batching, monkeypatch):
    from m3_memory import embedder_admin
    monkeypatch.setattr(embedder_admin, "stale_embed_servers",
                        lambda: [{"pid": 4242, "started": 1.0, "installed": 2.0}])
    setup_wizard._restart_stale_embed_server()
    labels = [label for label, _ in setup_wizard._ELEVATION.actions]
    argvs = [argv[-1] for _, argv in setup_wizard._ELEVATION.actions]
    assert "4242" in labels[0] and argvs == ["stop", "start"]


def test_stale_detection_compares_process_start_with_core_install(monkeypatch, tmp_path):
    """Started before the core was installed = still the previous binary."""
    from m3_memory import embedder_admin
    pkg = tmp_path / "site" / "m3_core_rs"
    pkg.mkdir(parents=True)
    exe = pkg / "m3-embed-server.exe"
    exe.write_bytes(b"MZ")
    rec = tmp_path / "site" / "m3_core_rs_windows_cuda-3.10.1.dist-info"
    rec.mkdir()
    (rec / "RECORD").write_text("x", encoding="utf-8")
    os.utime(rec / "RECORD", (1000.0, 1000.0))
    monkeypatch.setattr(embedder_admin, "_server_binary", lambda: exe)

    class _P:
        def __init__(self, pid, t):
            self.info = {"pid": pid, "name": "m3-embed-server.exe"}
            self._t = t
        def create_time(self):
            return self._t

    import psutil
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: [_P(1, 500.0), _P(2, 2000.0)])
    assert [s["pid"] for s in embedder_admin.stale_embed_servers()] == [1]
