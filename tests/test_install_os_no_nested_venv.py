"""install_os.py must never create a venv inside an installed wheel (#173).

#164 removed the leftover `site-packages/m3_memory/.venv` artifact. #142 stopped
`bin/setup_memory.py` from creating it. `install_os.py` was the remaining creator
— and it runs from `_post_install()` on EVERY install and upgrade, so the artifact
came back on the next `m3 install`. These tests pin the guard and the duplication.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_install_os():
    """Import install_os.py by path — it is a top-level script, not a package
    module, and importing it must not require m3 to be importable."""
    spec = importlib.util.spec_from_file_location(
        "_m3_install_os_under_test", _ROOT / "install_os.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def install_os():
    return _load_install_os()


@pytest.mark.parametrize("root,expected", [
    ("/usr/lib/python3.12/site-packages/m3_memory", True),
    ("/usr/lib/python3/dist-packages/m3_memory", True),
    (r"C:\Users\user\pipx\venvs\m3-memory\Lib\site-packages\m3_memory", True),
    ("/home/user/src/m3-memory", False),
    (r"C:\Users\user\.m3-dev\m3-memory", False),
    ("/home/user/site-packages-lookalike/m3", False),
])
def test_is_installed_layout_classifies(install_os, root, expected):
    assert install_os._is_installed_layout(root) is expected


def test_inlined_guard_matches_generate_configs(install_os):
    """The guard is DUPLICATED by necessity (install_os.py is stdlib-only and
    runs before m3 is importable). Duplication is allowed here only while the two
    agree — §10a: a copied predicate that drifts is the defect."""
    sys.path.insert(0, str(_ROOT / "bin"))
    try:
        from generate_configs import _is_installed_layout as canonical
    finally:
        sys.path.pop(0)

    for root in (
        "/usr/lib/python3.12/site-packages/m3_memory",
        "/usr/lib/python3/dist-packages/m3_memory",
        r"C:\Users\user\pipx\venvs\m3\Lib\site-packages\m3_memory",
        "/home/user/src/m3-memory",
        r"C:\Users\user\.m3-dev\m3-memory",
    ):
        assert install_os._is_installed_layout(root) == canonical(root), root


def test_venv_dir_is_none_on_installed_layout(install_os, monkeypatch):
    """VENV_DIR must be None when the script lives in site-packages, so no code
    path can pass it to venv.create()."""
    if install_os._is_installed_layout(install_os.BASE_DIR):
        assert install_os.VENV_DIR is None
    else:
        # Source checkout (the normal CI case): the venv path stays available.
        assert install_os.VENV_DIR is not None
        assert install_os.VENV_DIR.endswith(".venv")


def test_source_checkout_still_gets_a_venv_path(install_os):
    """The guard must not disable venv creation for real source checkouts —
    that is the supported dev path and #173 is not asking to remove it."""
    assert install_os._is_installed_layout("/home/user/src/m3-memory") is False


def test_no_unguarded_venv_create_in_source():
    """Belt and braces: the only venv.create call must sit under the not-
    installed branch. A future edit that hoists it out would silently restore
    the bug, and the parametrized tests above would still pass."""
    src = (_ROOT / "install_os.py").read_text(encoding="utf-8")
    assert src.count("venv.create(") == 1, "expected exactly one venv.create call"

    lines = src.splitlines()
    idx = next(i for i, l in enumerate(lines) if "venv.create(" in l)
    preceding = "\n".join(lines[max(0, idx - 12):idx])
    assert "_INSTALLED" in preceding or "else:" in preceding, (
        "venv.create() is no longer guarded by the _INSTALLED branch")
