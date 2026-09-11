"""The pip self-upgrade must never abort the install, or look like it did.

Observed on a real upgrade (2026-09-11, Windows): `m3 update` printed a red
multi-line dump and `[ERROR] Command failed`, then

    post-install:
      OS setup failed (code 1). Run it manually from ...install_os.py

Nothing was actually broken -- DB migrations, chatlog wiring, hooks and the
cognitive loop had all completed. Two separate defects produced it:

1. The call was ``pip.exe install --upgrade pip``. On Windows pip REFUSES to
   replace its own running executable and exits non-zero, telling you to use
   ``python -m pip`` instead. That is pip working as designed.
2. ``run_cmd`` was fatal for every command, so a cosmetic step took down the
   whole OS-setup phase.

A scary error for a step that changes nothing is not harmless: it teaches users
to ignore the next one, which might be real.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "m3_memory" / "install_os.py"


@pytest.fixture(scope="module")
def source() -> str:
    if not SRC.exists():  # pragma: no cover - packaging guard
        pytest.skip(f"{SRC} not present in this checkout")
    return SRC.read_text(encoding="utf-8")


def test_pip_self_upgrade_uses_python_m_pip(source: str) -> None:
    """`pip.exe install --upgrade pip` fails by design on Windows."""
    assert not re.search(r"run_cmd\(\s*\[\s*pip_exe\s*,\s*[\"']install[\"']\s*,"
                         r"\s*[\"']--upgrade[\"']\s*,\s*[\"']pip[\"']",
                         source), (
        "install_os.py upgrades pip via pip_exe. On Windows that exits non-zero "
        "with 'To modify pip, please run: <python> -m pip install --upgrade "
        "pip' -- use python_exe and `-m pip`."
    )
    assert re.search(r"run_cmd\(\s*\[\s*python_exe\s*,\s*[\"']-m[\"']\s*,"
                     r"\s*[\"']pip[\"']\s*,\s*[\"']install[\"']\s*,"
                     r"\s*[\"']--upgrade[\"']\s*,\s*[\"']pip[\"']", source), (
        "expected the pip self-upgrade to go through `python -m pip`"
    )


def test_pip_self_upgrade_is_not_fatal(source: str) -> None:
    """A convenience step must not abort the install.

    Every dependency install works on the pip a venv ships with, so failing to
    upgrade it changes nothing about whether m3 runs.
    """
    m = re.search(r"run_cmd\(\s*\[\s*python_exe\s*,\s*[\"']-m[\"']\s*,\s*[\"']pip[\"'].*?\)",
                  source, re.S)
    assert m, "could not find the pip self-upgrade call"
    assert "optional=True" in m.group(0), (
        "the pip self-upgrade is still fatal: run_cmd exits the process on a "
        "non-zero return, so a cosmetic failure aborts the whole OS-setup step"
    )


def test_run_cmd_supports_a_non_fatal_mode(source: str) -> None:
    """`optional=True` must actually change behaviour, not just be accepted."""
    m = re.search(r"def run_cmd\(.*?(?=\ndef )", source, re.S)
    assert m, "run_cmd not found"
    body = m.group(0)
    assert "optional" in body.split("\n")[0], "run_cmd takes no `optional` parameter"
    # It must return early rather than reaching sys.exit when optional.
    assert re.search(r"if optional:.*?return False", body, re.S), (
        "run_cmd accepts `optional` but still falls through to sys.exit"
    )
    # And the default must remain fatal -- silencing every failure would be worse
    # than the bug this fixes.
    assert "sys.exit(result.returncode)" in body, (
        "run_cmd no longer aborts on a non-optional failure; the default must "
        "stay fatal so a real error is not swallowed"
    )
