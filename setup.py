import os
import sys

# ── WMI-hang guard (Python 3.14 / Windows) ───────────────────────────────────
# On Windows + CPython 3.14, platform.uname() routes through a WMI query
# (_win32_ver/_wmi_query) that can hang indefinitely on a slow/contended WMI
# service. setuptools (msvc.py) and mypyc (build_setup.py) both call
# platform.system()/.machine() AT IMPORT TIME, so a plain `pip install` of this
# package can freeze before any of our code runs. We stub platform.uname() to a
# static, WMI-free result BEFORE importing setuptools below, so the build never
# touches WMI. No effect on non-Windows or non-3.14 environments where the WMI
# path isn't taken anyway.
if sys.platform == "win32":
    import platform as _platform
    try:
        _machine = os.environ.get("PROCESSOR_ARCHITECTURE", "AMD64")
        _safe_uname = _platform.uname_result(
            system="Windows", node="localhost", release="", version="",
            machine=_machine,
        )
        _platform.uname = lambda: _safe_uname
    except Exception:
        pass  # never let the guard itself break the build

from setuptools import setup

# mypyc compilation of the hot-path modules is a pure OPTIMIZATION — the package
# is fully functional as a pure-Python wheel without it. Skip it gracefully when:
#   - M3_SKIP_MYPYC is set (opt-out for constrained / cross / CI build envs), or
#   - mypyc can't be imported, or
#   - mypyc raises during compile (toolchain/version mismatch).
# A broken optional compiler must never block building the package.
ext_modules = []
if not os.environ.get("M3_SKIP_MYPYC"):
    try:
        from mypyc.build import mypycify
        # Only compile these stateless, high-frequency modules.
        ext_modules = mypycify([
            "bin/memory/util.py",
            "bin/memory/fts.py",
        ])
    # Catch BaseException, NOT just Exception. mypyc refuses to run against the
    # project's intentional `[tool.mypy] strict_optional = false` (mypyc
    # requires strict optional) and aborts via sys.exit() -> SystemExit, which
    # is a BaseException and slips past an `except Exception`. That made
    # `python -m build` fail outright instead of falling back to the pure-Python
    # wheel. A broken/incompatible optional compiler must NEVER block building
    # the package, so we fall back on any failure including SystemExit.
    except BaseException as e:  # ImportError, compile/toolchain errors, SystemExit
        import sys
        print(f"[setup] mypyc skipped ({type(e).__name__}: {e}); "
              "building pure-Python wheel.", file=sys.stderr)
        ext_modules = []

setup(
    ext_modules=ext_modules,
)
