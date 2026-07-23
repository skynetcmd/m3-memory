"""Tests for the stray-control-character checker (the PowerShell backtick trap).

PowerShell's escape character is the BACKTICK, not the backslash, so markdown
inline-code passed through a double-quoted PowerShell string is silently
mangled: the opening backtick escapes the word's first letter into a control
byte. `bytemuck` becomes \\x08ytemuck, `ring` becomes \\r + "ing".

Only words starting with b/n/r/t/a/f/v/0 are affected, which is what makes the
damage survive review — the rest of the document looks perfect. These tests
pin both halves: it must catch the real corruption, and it must not fire on
legitimate text (a false positive here blocks every commit).
"""
from __future__ import annotations

import os
import sys

BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bin")
if BIN not in sys.path:
    sys.path.insert(0, BIN)

from check_control_chars import main, scan  # noqa: E402


def _write(tmp_path, name, data: bytes):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


# ── catches the real corruption ──────────────────────────────────────────────

def test_detects_backspace_from_ps_backtick_b(tmp_path):
    """`bytemuck` through a double-quoted PS string -> \\x08ytemuck."""
    f = _write(tmp_path, "a.md", b"Replace struct.unpack with \x08ytemuck for zero-copy.\n")
    hits = scan(f)
    assert len(hits) == 1
    assert hits[0][2] == 0x08


def test_detects_vertical_tab_and_form_feed(tmp_path):
    """`v and `f are PS escapes too and have no legitimate use in our docs."""
    f = _write(tmp_path, "b.md", b"one\x0btwo\x0cthree\n")
    assert len(scan(f)) == 2


def test_reports_line_and_column(tmp_path):
    f = _write(tmp_path, "c.md", b"clean line\nsecond \x08line\n")
    (line, col, byte) = scan(f)[0]
    assert line == 2
    assert col == 8
    assert byte == 0x08


def test_exit_code_is_1_when_corrupt(tmp_path, capsys):
    f = _write(tmp_path, "d.md", b"use \x08ytemuck\n")
    assert main([f]) == 1
    out = capsys.readouterr().out
    assert "PowerShell" in out
    assert "\\x08" in out


def test_hint_names_the_consumed_letter(tmp_path, capsys):
    f = _write(tmp_path, "e.md", b"use \x08ytemuck\n")
    main([f])
    assert "'`b'" in capsys.readouterr().out


# ── must NOT false-positive ──────────────────────────────────────────────────

def test_tab_cr_lf_are_legitimate(tmp_path):
    """TAB, CR and LF are normal text — flagging them would block every commit."""
    f = _write(tmp_path, "f.md", b"col1\tcol2\r\nsecond line\n")
    assert scan(f) == []


def test_clean_markdown_with_backticks_passes(tmp_path, capsys):
    """The CORRECT form of the text that got mangled."""
    f = _write(tmp_path, "g.md", "Replace struct.unpack with `bytemuck`; use `ring`.\n".encode())
    assert main([f]) == 0
    assert capsys.readouterr().out == ""


def test_utf8_content_is_not_flagged(tmp_path):
    """Multi-byte UTF-8 must not be mistaken for control bytes."""
    f = _write(tmp_path, "h.md", "em—dash, café, 中文, ✅\n".encode())
    assert scan(f) == []


def test_non_text_extensions_are_skipped(tmp_path, capsys):
    """Binaries legitimately contain control bytes; only text files are scanned."""
    f = _write(tmp_path, "i.png", b"\x89PNG\r\n\x1a\n\x00\x00")
    assert main([f]) == 0
    assert capsys.readouterr().out == ""


def test_missing_file_does_not_crash(tmp_path):
    assert scan(str(tmp_path / "nope.md")) == []


def test_empty_arg_list_is_clean():
    assert main([]) == 0
