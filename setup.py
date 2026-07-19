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


# ── Ship the runnable payload INSIDE the wheel under m3_memory/ ────────────────
# A `pipx install m3-memory` user with no repo clone (possibly air-gapped) must
# get the full toolset without the `install-m3` GitHub fetch. bin/, docs/, etc.
# live at REPO ROOT (not under m3_memory/), and setuptools has no pyproject
# `force-include`, so we map each root tree in as a sub-package sourced from its
# root location: package_dir tells setuptools where a package's files LIVE, and
# package_data (with "*" + "**/*") pulls in every file, not just .py. Every
# subdir becomes its own package entry (setuptools maps one dir per package).
# installer.py resolves these from Path(m3_memory.__file__).parent / <component>
# (or their per-component $M3_PATH_* override).
def _payload_mapping():
    # (import-name-prefix, root-dir) — each root tree grafts under m3_memory/.
    # memory/migrations + memory/chatlog_migrations carry the schema .sql that
    # `m3 setup` applies; they live at REPO ROOT (not bin/), so ship them too.
    trees = [
        ("m3_memory.bin", "bin"),
        ("m3_memory.docs", "docs"),
        ("m3_memory._assets", "_assets"),
        ("m3_memory.examples", "examples"),
        # config/ carries the SLM classifier profiles (config/slm/*.yaml) that
        # the cognitive loop's entity-extraction/enrichment passes load by path
        # via slm_intent._profile_search_dirs (<root>/config/slm). Without this
        # graft they never ship in the wheel and every pass logs "SLM profile
        # not found" and no-ops.
        ("m3_memory.config", "config"),
        ("m3_memory.memory.migrations", os.path.join("memory", "migrations")),
        ("m3_memory.memory.chatlog_migrations", os.path.join("memory", "chatlog_migrations")),
    ]
    package_dir = {}
    packages = []
    package_data = {}
    for pkg_root, disk_root in trees:
        if not os.path.isdir(disk_root):
            continue
        for dirpath, dirnames, _files in os.walk(disk_root):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            rel = os.path.relpath(dirpath, disk_root)
            pkg = pkg_root if rel == "." else pkg_root + "." + rel.replace(os.sep, ".")
            package_dir[pkg] = dirpath.replace(os.sep, "/")
            packages.append(pkg)
            package_data[pkg] = ["*"]  # every file in this dir, any extension
    return package_dir, packages, package_data


_pkg_dir, _payload_pkgs, _pkg_data = _payload_mapping()

# install_os.py is a single ROOT-LEVEL file (the OS-level installer). It must
# land at m3_memory/install_os.py so _run_os_install (installer.py) finds it via
# bin_dir().parent. A tree-walk can't map a lone file, so stage it into a build
# dir under m3_memory/ and ship it as package_data of the top-level package.
# _run_os_install no-ops gracefully if it's absent, so a build without it still
# works — but ship it for completeness / air-gapped OS-install support.
def _stage_root_file(src, pkg="m3_memory"):
    import shutil
    if not os.path.isfile(src):
        return
    dst = os.path.join(pkg, os.path.basename(src))
    # Only copy if missing or stale (build is repeatable / idempotent).
    if not os.path.isfile(dst) or os.path.getmtime(src) > os.path.getmtime(dst):
        shutil.copy2(src, dst)
    _pkg_data.setdefault(pkg, []).append(os.path.basename(src))


_stage_root_file("install_os.py")

# mypyc compilation of the hot-path modules is a pure OPTIMIZATION — the package
# is fully functional as a pure-Python wheel without it. Skip it gracefully when:
#   - M3_SKIP_MYPYC is set (opt-out for constrained / cross / CI build envs), or
#   - mypyc can't be imported, or
#   - mypyc raises during compile (toolchain/version mismatch).
# A broken optional compiler must never block building the package.
ext_modules = []
if not os.environ.get("M3_SKIP_MYPYC"):
    try:
        # Compile these stateless, high-frequency modules — but ONLY from the
        # STAGED in-package location. The payload now ships under m3_memory/bin/
        # (package_dir maps root bin/ -> m3_memory/bin/), so the compiled module
        # name must be `m3_memory.bin.memory.*` to match where the SOURCE ships.
        # Compiling the repo-root `bin/memory/util.py` would name the extension
        # `bin.memory.util` — a stray top-level `bin` package that mismatches the
        # shipped source (a silent orphaned/polluting .pyd). So stage the two
        # modules into m3_memory/bin/memory/ first and mypycify the staged paths.
        # If staging fails, fall through to the pure-Python wheel (mypyc is a pure
        # optimization; §1 — the package is fully functional without it).
        import shutil as _sh

        from mypyc.build import mypycify
        _staged = []
        for _rel in ("memory/util.py", "memory/fts.py"):
            _src = os.path.join("bin", _rel)
            _dst = os.path.join("m3_memory", "bin", _rel)
            os.makedirs(os.path.dirname(_dst), exist_ok=True)
            if not os.path.isfile(_dst) or os.path.getmtime(_src) > os.path.getmtime(_dst):
                _sh.copy2(_src, _dst)
            _staged.append(_dst.replace(os.sep, "/"))
        ext_modules = mypycify(_staged)
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

# Discover the real python packages (m3_memory + subpackages) the same way
# pyproject's [tool.setuptools.packages.find] would, then ADD the payload trees.
# Passing `packages`/`package_dir`/`package_data` here supersedes the pyproject
# `find`, so we must reproduce its include/exclude.
#
# `m3_memory.integrations.langchain` is an IMPORTABLE subpackage (it has its own
# __init__.py, as does the `integrations` parent) and must ship as code so the
# documented `from m3_memory.langchain import Memory/M3Store/M3Saver` path works
# for installed (non-dev) users. Only `hermes` is excluded — it ships as package
# *data* (loaded by path, not imported), added to package_data below.
from setuptools import find_packages

_code_pkgs = find_packages(
    where=".", include=["m3_memory*"], exclude=["m3_memory.integrations.hermes*"]
)

# package_data for the CODE package: the mcp examples + the vendored Hermes
# provider (which ships as data, not an importable package). Previously in
# pyproject's [tool.setuptools.package-data]; moved here since setup.py now owns
# packages/package_dir/package_data (all three must come from one source).
_code_data = {
    "m3_memory": [
        "mcp.json.example",
        "mcp-server.json",
        "integrations/hermes/*.py",
        "integrations/hermes/*.yaml",
        "integrations/hermes/*.md",
    ],
    # The crewai integration ships as an importable subpackage (auto-discovered by
    # find_packages); its README is data, included so `pip install`ed users get it.
    "m3_memory.integrations.crewai": ["*.md"],
    # Same for the pydantic-ai integration subpackage.
    "m3_memory.integrations.pydantic_ai": ["*.md"],
}
_pkg_data.update(_code_data)

setup(
    ext_modules=ext_modules,
    packages=_code_pkgs + _payload_pkgs,
    package_dir=_pkg_dir,
    package_data=_pkg_data,
)
