"""The conftest detector must fire on an incoherent namespace, and blame fairly.

Touches NO real `memory.*` module. Two earlier tests here did, and both were
wrong to: one asserted the live namespace was healthy, which is a global claim
that depends on every test that ran before it (it failed on the second PG run,
reporting the real namespace as present-without-`backends`), and the package
guard it checked is already covered by
`tests/test_backends_dialect_not_rebound.py`.

Exercised against a SYNTHETIC package, never the real `memory.*`. Churning the
real modules to test this is the very hazard the detector is about: m3 memory
`25df967b` records that `memory.*` sys.modules manipulation leaks across tests,
and an earlier version of this file did exactly that and crashed the Windows
interpreter at exit (`0xC0000005`) after a clean summary line.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from conftest import _memory_namespace_problem as _problem  # noqa: E402
from conftest import _report_namespace_breakage as _report  # noqa: E402

PKG = "_m3_probe_pkg"


def _install(*, with_backends: bool, dialect_callable: bool) -> None:
    pkg = ModuleType(PKG)
    if with_backends:
        backends = ModuleType(f"{PKG}.backends")
        backends.dialect = (lambda: "dialect") if dialect_callable else ModuleType("d")
        pkg.backends = backends
        sys.modules[f"{PKG}.backends"] = backends
    sys.modules[PKG] = pkg


@pytest.fixture(autouse=True)
def _clean_probe():
    yield
    for name in [n for n in list(sys.modules) if n == PKG or n.startswith(PKG + ".")]:
        del sys.modules[name]


def test_a_healthy_namespace_passes():
    _install(with_backends=True, dialect_callable=True)
    assert _problem(PKG) is None


def test_an_absent_package_is_fine():
    """That is the purge having done its job, not a fault."""
    assert _problem(PKG) is None


def test_a_missing_backends_attribute_is_caught():
    _install(with_backends=False, dialect_callable=True)
    assert _problem(PKG) is not None
    with pytest.raises(RuntimeError, match="WITHOUT a `backends` attribute"):
        _report(entered_coherent=True, package=PKG)
    assert PKG not in sys.modules, "must purge so the breakage cannot cascade"


def test_a_shadowed_dialect_is_caught():
    _install(with_backends=True, dialect_callable=False)
    with pytest.raises(RuntimeError, match="not the accessor function"):
        _report(entered_coherent=True, package=PKG)


def test_a_test_that_INHERITED_the_breakage_is_not_blamed():
    """One breakage must not fail every test that follows it.

    The first version blamed whichever test was running, turning a single cause
    into 2819 teardown errors on the SQLite lane.
    """
    _install(with_backends=False, dialect_callable=True)
    _report(entered_coherent=False, package=PKG)      # inherited: must NOT raise
    assert PKG not in sys.modules, "it must still repair the namespace"


def test_the_message_names_where_to_look():
    """A diagnostic that does not say what to change costs a second round."""
    _install(with_backends=False, dialect_callable=True)
    with pytest.raises(RuntimeError) as e:
        _report(entered_coherent=True, package=PKG)
    assert ".claude/rules/test-sandbox.md" in str(e.value)
