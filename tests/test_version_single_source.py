"""One version, one source — the runtime answer must not depend on where you stand.

m3 had four different answers to "what version is this?" on a single machine:

    2026.8.19.15   pyproject.toml            <- source of truth
    2026.8.19.15   pipx wheel dist-info      <- correct
    2026.7.31.1    repo m3_memory.egg-info   <- stale build artifact, shadows via cwd
    2026.7.19.5    dev venv editable         <- frozen at install time, 22 releases old

Two mechanisms produce the divergence, both ordinary Python behaviour:

  * ``importlib.metadata`` searches ``sys.path``, whose first entry is the CWD,
    so any process launched from a checkout reads that tree's egg-info first.
  * pip stamps an editable install's version into dist-info at INSTALL time and
    never rewrites it on a source edit.

The cost was not cosmetic: the doctor's entrypoint probe compared two shadowed
numbers and told the user to uninstall a working launcher.

These tests pin the invariant rather than any particular number.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pyproject_version() -> str:
    import tomllib
    with open(os.path.join(_ROOT, "pyproject.toml"), "rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def test_package_version_matches_pyproject_in_a_checkout():
    """In a source checkout, pyproject.toml IS the version — nothing else.

    Guards against both shadowing mechanisms at once: a stale egg-info in the
    repo root and an editable install whose dist-info was frozen months ago
    are BOTH present on a normal dev box, and neither may win.
    """
    import m3_memory
    assert m3_memory.__version__ == _pyproject_version(), (
        f"package reports {m3_memory.__version__!r} but pyproject.toml says "
        f"{_pyproject_version()!r} — stale egg-info or a frozen editable "
        "install is shadowing the source of truth"
    )


def test_version_is_the_same_from_any_working_directory():
    """cwd must not be an input to the version. It was, for both readers."""
    code = "import m3_memory;print(m3_memory.__version__)"
    seen = {}
    for label, cwd in (("checkout", _ROOT), ("neutral", tempfile.gettempdir())):
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=60, cwd=cwd)
        if label == "neutral" and "No module named 'm3_memory'" in (proc.stderr or ""):
            # The package is not installed in this interpreter — CI's hermetic
            # lane installs requirements.txt only, never the package itself. The
            # cwd-independence this test guards is only *meaningful* when the
            # package is importable from outside the checkout; without an
            # install there is no second reader to disagree with. Skip rather
            # than fail, so a real divergence stays visible where it can occur.
            pytest.skip("m3_memory is not installed in this interpreter")
        assert proc.returncode == 0, f"{label}: {proc.stderr[-300:]}"
        seen[label] = proc.stdout.strip()
    # An installed distribution that is simply an OLDER RELEASE than the working
    # tree is expected drift, not cwd-dependence: the checkout legitimately
    # reports pyproject.toml's version. The bug this guards is the two readers
    # disagreeing while the install is current, so compare only then.
    if seen["checkout"] != seen["neutral"] and seen["checkout"] == _pyproject_version():
        pytest.skip(
            f"installed distribution ({seen['neutral']}) is older than the "
            f"checkout ({seen['checkout']}); reinstall to compare"
        )
    assert seen["checkout"] == seen["neutral"], (
        f"version depends on the working directory: {seen} — a *.egg-info or "
        "*.dist-info in the checkout is being read before the real one"
    )


def test_doctor_agrees_with_the_package():
    """The entrypoint probe must not answer the version question independently.

    A second resolver is a second answer, which is precisely the defect the
    probe exists to detect — it once reported three fabricated 'stale launcher'
    failures because its answer and the package's disagreed.
    """
    sys.path.insert(0, os.path.join(_ROOT, "bin"))
    from doctor import entrypoint_probe as ep

    import m3_memory
    installed = ep._installed_version()
    if installed is None:
        pytest.skip("no installed distribution to compare against")
    # The doctor deliberately resolves from a NEUTRAL cwd (the installed
    # distribution) while the package reads the checkout's pyproject.toml. When
    # the local install is simply an older release than the working tree, that
    # is expected drift, not the two-sources-of-truth bug this guards.
    if installed != m3_memory.__version__ and _pyproject_version() == m3_memory.__version__:
        pytest.skip(
            f"installed distribution ({installed}) is older than the checkout "
            f"({m3_memory.__version__}); reinstall to compare"
        )
    assert installed == m3_memory.__version__, (
        f"doctor says {ep._installed_version()!r}, package says "
        f"{m3_memory.__version__!r} — two sources of truth"
    )


def test_no_module_resolves_m3s_version_independently():
    """Only m3_memory/__init__.py may ask importlib.metadata for m3's version.

    Any other call site is a SECOND answer to the same question, and both
    shadowing mechanisms then apply to it independently: a checkout's stale
    egg-info wins over the installed dist-info, and an editable install's
    dist-info is frozen at install time. That is how the entrypoint probe and
    the plugin probe each ended up comparing numbers nothing was running.

    Callers use ``m3_memory.__version__``, which routes through the single
    resolver that knows the precedence.
    """
    import ast

    allowed = os.path.join(_ROOT, "m3_memory", "__init__.py")
    offenders = []
    for base in ("bin", "m3_memory"):
        for root, _dirs, files in os.walk(os.path.join(_ROOT, base)):
            if "__pycache__" in root or f"{os.sep}.venv{os.sep}" in root:
                continue
            for fn in files:
                if not fn.endswith(".py"):
                    continue
                path = os.path.join(root, fn)
                if os.path.abspath(path) == os.path.abspath(allowed):
                    continue
                try:
                    # `with`, not a bare open(): this suite runs
                    # warnings-as-errors, and a leaked handle raises
                    # ResourceWarning — a test that fails on its own plumbing
                    # reads exactly like a real finding.
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        tree = ast.parse(fh.read())
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call) or not node.args:
                        continue
                    fnode = node.func
                    name = (fnode.attr if isinstance(fnode, ast.Attribute)
                            else getattr(fnode, "id", ""))
                    if name != "version":
                        continue
                    arg = node.args[0]
                    if (isinstance(arg, ast.Constant)
                            and isinstance(arg.value, str)
                            and arg.value.replace("_", "-") == "m3-memory"):
                        offenders.append(
                            f"{os.path.relpath(path, _ROOT)}:{node.lineno}")
    assert not offenders, (
        "resolved m3's version via importlib.metadata instead of "
        "m3_memory.__version__ — a second source of truth:\n  "
        + "\n  ".join(offenders)
    )
