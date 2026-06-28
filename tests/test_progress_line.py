"""_progress collapses long per-item output to one self-updating TTY line.

On a TTY each call rewrites the same line (carriage-return + clear-to-EOL) so a
long "1/N ... 2/N ..." loop shows as one updating status line. When stdout is
NOT a TTY (piped/redirected/non-interactive SSH/CI) it falls back to a normal
newline print so the full log is preserved and grep-able.
"""
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))


class _FakeTTY(io.StringIO):
    """StringIO that reports isatty() per the flag, to drive _progress branches."""

    def __init__(self, is_tty: bool):
        super().__init__()
        self._tty = is_tty

    def isatty(self) -> bool:  # noqa: D401
        return self._tty


def _load_progress():
    import importlib.util

    # Stub the heavy bridge import so the module loads standalone.
    import types

    sys.modules.setdefault(
        "memory_bridge",
        types.SimpleNamespace(memory_delete=lambda *a, **k: None, memory_write=lambda *a, **k: None),
    )
    spec = importlib.util.spec_from_file_location(
        "eai_progress", Path(__file__).resolve().parents[1] / "bin" / "embed_agent_instructions.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._progress


def test_tty_rewrites_in_place(monkeypatch):
    progress = _load_progress()
    fake = _FakeTTY(is_tty=True)
    monkeypatch.setattr(sys, "stdout", fake)
    monkeypatch.delenv("NO_COLOR", raising=False)
    progress("1/3 alpha")
    out = fake.getvalue()
    assert out.startswith("\r")          # carriage-return: rewrite from col 0
    assert "\033[K" in out                # clear-to-end-of-line
    assert not out.endswith("\n")         # mid-loop: no newline (stays on one line)


def test_tty_done_commits_newline(monkeypatch):
    progress = _load_progress()
    fake = _FakeTTY(is_tty=True)
    monkeypatch.setattr(sys, "stdout", fake)
    monkeypatch.delenv("NO_COLOR", raising=False)
    progress("3/3 gamma", done=True)
    assert fake.getvalue().endswith("\n")  # final call commits the newline


def test_non_tty_prints_full_lines(monkeypatch):
    progress = _load_progress()
    fake = _FakeTTY(is_tty=False)
    monkeypatch.setattr(sys, "stdout", fake)
    progress("1/3 alpha")
    out = fake.getvalue()
    assert "\r" not in out                 # no carriage-return tricks when piped
    assert out.endswith("\n")              # one full line, preserved for logs


def test_no_color_forces_plain(monkeypatch):
    progress = _load_progress()
    fake = _FakeTTY(is_tty=True)            # TTY, but NO_COLOR set
    monkeypatch.setattr(sys, "stdout", fake)
    monkeypatch.setenv("NO_COLOR", "1")
    progress("2/3 beta")
    out = fake.getvalue()
    assert "\r" not in out                 # NO_COLOR also disables the rewrite
    assert out.endswith("\n")
