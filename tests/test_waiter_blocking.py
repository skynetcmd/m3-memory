
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path("bin").resolve()))
from m3_halt import _WRITER_CMDLINE_SIGNATURES, NON_BLOCKING_ROLES


def test_waiter_is_visible_but_non_blocking():
    assert "waiter" in _WRITER_CMDLINE_SIGNATURES, "Waiter must be visible to cmdline scan"
    assert "waiter" in NON_BLOCKING_ROLES, "Waiter must NOT block exclusive ops (it holds no store)"

