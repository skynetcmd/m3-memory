"""The cognitive loop writes each log record to --log-file once.

Hazard: when stdout already is the log file (Windows --background re-exec,
launchd StandardOutPath), adding a FileHandler for the same path writes every
record twice through two independent handles, duplicating and splitting lines.
"""
from __future__ import annotations

import logging
import os
import sys

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

import m3_cognitive_loop as loop  # noqa: E402


def _logger(name):
    lg = logging.getLogger(name)
    lg.propagate = False
    lg.handlers.clear()
    lg.setLevel(logging.INFO)
    return lg


def test_no_second_handle_when_stdout_is_the_log_file(tmp_path, monkeypatch):
    log = tmp_path / "loop.log"
    with open(log, "a", encoding="utf-8") as out:
        monkeypatch.setattr(sys, "stdout", out)
        lg = _logger("loop-once-a")
        lg.addHandler(logging.StreamHandler(out))
        assert loop._attach_log_file(str(log), lg) is False
        lg.info("one record")
    assert log.read_text(encoding="utf-8").count("one record") == 1


def test_file_handler_added_when_stdout_is_elsewhere(tmp_path, monkeypatch):
    log, other = tmp_path / "loop.log", tmp_path / "journal.txt"
    with open(other, "a", encoding="utf-8") as out:
        monkeypatch.setattr(sys, "stdout", out)
        lg = _logger("loop-once-b")
        try:
            assert loop._attach_log_file(str(log), lg) is True
            lg.info("to the file")
        finally:
            for h in lg.handlers:
                h.close()
    assert "to the file" in log.read_text(encoding="utf-8")


def test_undeterminable_stdout_keeps_the_file_handler(tmp_path, monkeypatch):
    """No fileno (pytest capture, a closed stream): never lose the file log."""
    class _NoFd:
        def fileno(self):
            raise OSError("no fd")

    monkeypatch.setattr(sys, "stdout", _NoFd())
    assert loop._stdout_is_file(str(tmp_path / "x.log")) is False
