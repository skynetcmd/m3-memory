"""The chatlog shell hooks must prefer pythonw.exe over python.exe on Windows.

Regression origin (2026-09-13): the Stop hook fires on every turn, and its
shell wrapper runs up to three interpreters per fire (usable-probe, envelope
parse, ingest exec). Every Windows candidate named python.exe — the CONSOLE
build — so each fire allocated a conhost and flashed a focus-stealing window
every few seconds.

The .py hooks already pass CREATE_NO_WINDOW for exactly this reason. A shell
script has no such flag, so the only lever is choosing the windowless binary.
This guard keeps the two halves consistent: a future edit that adds a Windows
candidate must add the pythonw form too.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_HOOKS = Path(__file__).resolve().parent.parent / "bin" / "hooks" / "chatlog"

# Every .sh wrapper that resolves a Windows interpreter.
_WRAPPERS = sorted(p for p in _HOOKS.glob("*.sh")
                   if "Scripts/python" in p.read_text(encoding="utf-8"))


def test_wrappers_exist():
    """A rename that empties this list would make every assertion below vacuous
    — the classic guard-that-cannot-fail (§12c)."""
    assert _WRAPPERS, f"no shell wrappers with Windows candidates under {_HOOKS}"


@pytest.mark.parametrize("wrapper", _WRAPPERS, ids=lambda p: p.name)
def test_every_python_exe_candidate_has_a_pythonw_sibling(wrapper: Path):
    """For each `.../Scripts/python.exe` candidate there must be a matching
    `.../Scripts/pythonw.exe` candidate at the SAME location."""
    text = wrapper.read_text(encoding="utf-8")
    console = set(re.findall(r'([^\s"]*)/Scripts/python\.exe', text))
    windowless = set(re.findall(r'([^\s"]*)/Scripts/pythonw\.exe', text))
    missing = console - windowless
    assert not missing, (
        f"{wrapper.name}: these locations offer python.exe (console build, "
        f"flashes a window) with no pythonw.exe sibling: {sorted(missing)}"
    )


@pytest.mark.parametrize("wrapper", _WRAPPERS, ids=lambda p: p.name)
def test_pythonw_is_tried_before_python_exe(wrapper: Path):
    """Order matters: the first usable candidate wins, so pythonw.exe must be
    probed first or the console build is still what runs."""
    text = wrapper.read_text(encoding="utf-8")
    for location in set(re.findall(r'([^\s"]*)/Scripts/pythonw\.exe', text)):
        w = text.find(f"{location}/Scripts/pythonw.exe")
        c = text.find(f"{location}/Scripts/python.exe")
        if c == -1:
            continue
        assert w < c, (
            f"{wrapper.name}: python.exe is tried before pythonw.exe at "
            f"{location} — the console build would win and flash a window"
        )


@pytest.mark.parametrize("wrapper", _WRAPPERS, ids=lambda p: p.name)
def test_console_fallback_is_retained(wrapper: Path):
    """pythonw.exe REPLACES nothing: a layout that ships only python.exe must
    still resolve, so the console candidate stays as the next fallback."""
    text = wrapper.read_text(encoding="utf-8")
    assert "/Scripts/python.exe" in text, (
        f"{wrapper.name}: dropped the python.exe fallback entirely — a host "
        f"without pythonw.exe would lose chatlog capture"
    )


@pytest.mark.parametrize("wrapper", _WRAPPERS, ids=lambda p: p.name)
def test_wrapper_is_lf_and_parses(wrapper: Path):
    """CRLF in a shell script breaks the shebang on Unix; and a syntax error
    here silently kills capture, which is the failure m3 treats as an
    emergency."""
    raw = wrapper.read_bytes()
    assert b"\r\n" not in raw, f"{wrapper.name} has CRLF line endings"
