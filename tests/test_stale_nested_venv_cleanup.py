"""An upgrade must remove the pre-#142 venv nested inside the payload.

#142 stopped the installer CREATING a venv inside
``site-packages/m3_memory``. Nothing removed the one an older install left
behind -- and it is not inert. It is a ``--copies`` venv pinned to an exact
interpreter patch version.

Measured on macOS 2026-09-12: Homebrew moved ``python@3.14`` from 3.14.6 to
3.14.7 and two launchd services whose ``ProgramArguments`` still pointed into
that venv died on every launch::

    dyld: Library not loaded: .../Cellar/python@3.14/3.14.6/...

exit signal 6, silently, while ``m3 status`` reported HEALTHY -- because the
check confirmed the plist was REGISTERED, never that the process ran.

The artifact is present on the Windows box too (verified 2026-09-12), dormant
only because its tasks were repointed by hand.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_SRC = _HERE.parent / "bin" / "setup_memory.py"


def _load_helpers():
    """Exec ONLY the two helpers.

    setup_memory.py runs its install at module scope -- importing it would
    attempt a real install as a side effect of collecting this test.
    """
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    ns: dict = {"pathlib": pathlib, "sys": sys, "os": os}
    wanted = {"_nested_venv_path", "remove_stale_nested_venv"}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in wanted:
            exec(  # nosec B102 - executing this repo's own parsed source
                compile(ast.Module(body=[node], type_ignores=[]), "setup_memory", "exec"),
                ns,
            )
    missing = wanted - ns.keys()
    assert not missing, f"setup_memory.py is missing {sorted(missing)}"
    return ns


def test_the_cleanup_exists_and_is_wired_into_the_installed_path():
    """A helper nobody calls is a declared limit with no enforcement site (§3).

    It must run on the INSTALLED branch specifically: a source checkout's
    ``.venv`` is correct and must never be swept.
    """
    src = _SRC.read_text(encoding="utf-8")
    assert "def remove_stale_nested_venv" in src
    idx = src.index("if _INSTALLED:")
    window = src[idx:idx + 500]
    assert "remove_stale_nested_venv()" in window, (
        "cleanup is not called on the installed-layout branch, so the artifact "
        "survives every upgrade"
    )


def test_a_source_checkout_venv_is_never_swept():
    """The safety property.

    `_nested_venv_path` resolves through the IMPORTED m3_memory package, so in a
    dev checkout it finds the checkout (whose `.venv` is legitimate) and the
    `_INSTALLED` gate means the sweep never runs there anyway. Both halves
    matter: resolution alone could still point at a real venv.
    """
    src = _SRC.read_text(encoding="utf-8")
    assert "import m3_memory" in src, (
        "resolution must go through the package, not a hardcoded path"
    )
    # The call site must be gated on _INSTALLED, not unconditional.
    idx = src.index("remove_stale_nested_venv()")
    assert "_INSTALLED" in src[:idx], "the sweep is not gated on an installed layout"


def test_failure_to_remove_is_reported_not_raised():
    """This runs during an upgrade. A stale venv is a LATENT hazard; a broken
    upgrade is an immediate one, so the cleanup must never be the thing that
    fails the run -- but it must say so loudly when it cannot delete."""
    # Read the SOURCE TEXT, not inspect.getsource: these helpers are built by
    # exec() from a parsed AST and have no source file, so getsource raises
    # OSError. The file on disk is the thing under test anyway.
    full = _SRC.read_text(encoding="utf-8")
    start = full.index("def remove_stale_nested_venv")
    src = full[start:start + 2200]
    assert "except Exception" in src, "a delete failure must not propagate"
    assert "could NOT remove" in src, (
        "a swallowed delete failure leaves the hazard in place silently"
    )
    assert "by hand" in src, "the operator is not told what to do about it"


def test_returns_none_when_there_is_nothing_to_remove():
    """Silent on a clean system. A cleanup that announces itself on every
    upgrade where it did nothing is noise, and §3 treats a message that fires
    when nothing is wrong as a violation in its own right."""
    ns = _load_helpers()
    # In this dev checkout there is no nested venv under the imported package.
    assert ns["remove_stale_nested_venv"](dry_run=True) in (None,), (
        "reported an action on a system with no nested venv"
    )


def test_dry_run_does_not_delete(tmp_path, monkeypatch):
    """dry_run must describe, never act -- it is what an operator reaches for
    when they want to know whether the sweep would touch anything."""
    ns = _load_helpers()
    fake = tmp_path / "m3_memory" / ".venv"
    fake.mkdir(parents=True)
    (fake / "marker").write_text("x", encoding="utf-8")

    monkeypatch.setitem(ns, "_nested_venv_path", lambda: fake)
    out = ns["remove_stale_nested_venv"](dry_run=True)
    assert out and "would remove" in out, out
    assert fake.is_dir(), "dry_run deleted the directory"
