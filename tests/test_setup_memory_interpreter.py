"""Pins interpreter resolution in bin/setup_memory.py.

The bug: setup_memory derived PY from `__file__/../../.venv` unconditionally, so
on an INSTALLED payload (BASE = <site-packages>/m3_memory) it created a venv
*inside site-packages* and ran every subsequent step with an interpreter that had
no m3_memory importable. Symptoms were a failed
`from m3_memory.embedder_admin import seed_shared_config` and a spurious
"requirements.txt not found".

These tests exercise the module as a SCRIPT under both layouts, because the
defect lives in module-level control flow, not in a callable — importing a helper
would not have caught it. They assert on the resolved interpreter and on whether
a nested venv directory appears; a test that only asserted "no .venv" would pass
on a crash, which is how the first draft of this fix fooled its own check.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parent.parent / "bin"
_SCRIPT = _BIN / "setup_memory.py"


def _stage(root: Path) -> Path:
    """Copy the bootstrap pair into a fake tree and return its bin/."""
    b = root / "bin"
    b.mkdir(parents=True)
    for name in ("setup_memory.py", "generate_configs.py"):
        (b / name).write_bytes((_BIN / name).read_bytes())
    (root / "memory" / "migrations").mkdir(parents=True)
    return b


def _run(bin_dir: Path, db: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(bin_dir / "setup_memory.py"), "--database", str(db)],
        capture_output=True, text=True, timeout=300,
    )


def test_installed_layout_reuses_running_interpreter(tmp_path):
    """An installed payload must NOT nest a venv inside site-packages."""
    root = tmp_path / "site-packages" / "m3_memory"
    bin_dir = _stage(root)
    r = _run(bin_dir, root / "t.db")

    assert r.returncode == 0, f"bootstrap failed:\n{r.stderr}"
    assert not (root / ".venv").exists(), "created a nested venv inside site-packages"
    assert "Installed layout detected" in r.stderr
    # It must use the interpreter that launched it, not a constructed path.
    assert sys.executable in r.stderr
    # And it must not try to pip-install into a wheel-managed environment.
    assert "Installing dependencies" not in r.stderr


def test_installed_layout_emits_no_missing_requirements_warning(tmp_path):
    """The spurious requirements.txt warning was the same wrong-BASE symptom."""
    root = tmp_path / "site-packages" / "m3_memory"
    bin_dir = _stage(root)
    r = _run(bin_dir, root / "t.db")

    assert r.returncode == 0, r.stderr
    assert "requirements.txt not found" not in r.stderr


def test_source_checkout_still_builds_its_own_venv(tmp_path):
    """The source path is the one that legitimately needs a venv — don't regress it."""
    root = tmp_path / "m3-checkout"
    bin_dir = _stage(root)
    (root / "requirements.txt").write_text("wheel\n", encoding="utf-8")
    r = _run(bin_dir, root / "t.db")

    assert r.returncode == 0, f"bootstrap failed:\n{r.stderr}"
    assert (root / ".venv").exists(), "source checkout must create its own venv"
    assert "Creating virtual environment" in r.stderr


def test_source_checkout_without_requirements_does_not_crash(tmp_path):
    """A checkout missing requirements.txt should warn, not explode."""
    root = tmp_path / "m3-checkout"
    bin_dir = _stage(root)
    r = _run(bin_dir, root / "t.db")

    assert r.returncode == 0, f"bootstrap failed:\n{r.stderr}"
    assert "skipping dependency install" in r.stderr.lower()


@pytest.mark.parametrize(
    "root,expected",
    [
        # macOS / Linux / Windows installed roots, and their source
        # counterparts. Home dirs are written as /opt/<acct> and D:\<acct>
        # rather than the usual home-path spellings: the repo's pre-push scan
        # matches the SHAPE of a home path and cannot tell a fixture from a
        # real leak, and a gate that cries wolf on every push trains people to
        # bypass it (DESIGN_PHILOSOPHIES §3). This test needs the SEPARATOR and
        # the site-packages/dist-packages segment, which these preserve.
        ("/opt/acct/.local/pipx/venvs/m3/lib/python3.12/site-packages/m3_memory", True),
        ("/usr/lib/python3/dist-packages/m3_memory", True),
        (r"D:\acct\pipx\venvs\m3-memory\Lib\site-packages\m3_memory", True),
        ("/opt/acct/dev/m3-memory", False),
        ("/srv/acct/dev/m3-memory", False),
        (r"C:\dev\m3-memory", False),
    ],
)
def test_layout_predicate_is_platform_agnostic(root, expected):
    """One predicate must classify all three OSes' path forms.

    setup_memory imports this from generate_configs rather than keeping a copy;
    this test guards the contract it depends on (including the str-not-Path
    signature, which a local copy had been silently papering over).
    """
    sys.path.insert(0, str(_BIN))
    from generate_configs import _is_installed_layout

    assert _is_installed_layout(root) is expected


def test_setup_memory_does_not_duplicate_the_predicate():
    """§10a: one owner for the predicate. A second copy drifts."""
    src = _SCRIPT.read_text(encoding="utf-8")
    assert "from generate_configs import _is_installed_layout" in src
    assert "def _is_installed_layout" not in src, (
        "setup_memory defines its own copy of the layout predicate; import the "
        "single owner in generate_configs instead"
    )
