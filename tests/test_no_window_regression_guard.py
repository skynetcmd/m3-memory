"""Regression guard: background/unattended code must not flash a console window.

DESIGN_PHILOSOPHIES §1 (degrade cleanly across the OS matrix) + §8 (don't disturb
the host): a process that runs UNATTENDED — the cognitive loop, its telemetry
probes, scheduled tasks, and chatlog hooks — spawns console subprocesses
(nvidia-smi, powershell, wmic, git, python, ...). On Windows a console-subsystem
child FLASHES a window and steals focus every time it runs. On the per-cycle
governor path that is a constant, focus-stealing nuisance for every Windows user.

The fix is to pass `creationflags=CREATE_NO_WINDOW` (via the shared
_task_runtime.no_window_kwargs helper, or an equivalent local helper) on every
such spawn. This test makes that non-optional: it AST-walks the unattended
entrypoints and fails if any subprocess spawn lacks a no-window argument — so a
future contributor can't silently reintroduce the flash for users.

Deliberately OUT OF SCOPE (per the intended UX): interactive entrypoints — the
mission_control dashboard, setup/install wizards, dev CLIs — SHOULD show a
window; the user invoked them and is watching. Only unattended paths are guarded.
"""
from __future__ import annotations

import ast
import os

import pytest

_BIN = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "bin"))

# Commands that are POSIX-only (or otherwise never a Windows console app), so a
# missing no-window flag can't flash on Windows. Keyed on argv[0] literal.
_POSIX_ONLY_CMDS = {
    "crontab", "launchctl", "systemctl", "sysctl", "ioreg", "sensors",
    "sh", "/bin/sh", "bash", "osascript", "defaults", "sw_vers", "uname",
}

# The spawn call names we police.
_SPAWN_ATTRS = {"run", "Popen", "call", "check_call", "check_output"}

# Kwarg / splat names that satisfy the no-window requirement.
_NOWINDOW_KWARG = "creationflags"
_NOWINDOW_HELPERS = {"no_window_kwargs", "_no_window", "_nw"}


def _unattended_files() -> list[str]:
    """Derive the set of unattended entrypoints from the source of truth rather
    than a hand-maintained list (so a new scheduled task is auto-covered):

      * every script referenced by install_schedules' schedule specs,
      * every chatlog hook,
      * the cognitive loop + the telemetry/probe modules it calls per cycle.
    """
    import sys
    if _BIN not in sys.path:
        sys.path.insert(0, _BIN)
    import install_schedules as isch

    files: set[str] = set()

    # Scripts run by scheduled tasks (args[0] of each spec is the .py path).
    for spec in isch.get_schedule_specs(os.path.dirname(_BIN)):
        script = spec["args"][0]
        if str(script).endswith(".py") and os.path.exists(script):
            files.add(os.path.abspath(script))

    # Chatlog hooks (fire unattended per session/compact).
    hooks_dir = os.path.join(_BIN, "hooks", "chatlog")
    if os.path.isdir(hooks_dir):
        for name in os.listdir(hooks_dir):
            if name.endswith(".py") and not name.startswith("_"):
                files.add(os.path.join(hooks_dir, name))

    # The cognitive loop + the modules it polls every cycle.
    for name in ("m3_cognitive_loop.py", "m3_sdk.py", "thermal_utils.py",
                 "enrichment_state.py", "m3_enrich_batch_parallel.py"):
        p = os.path.join(_BIN, name)
        if os.path.exists(p):
            files.add(p)

    # ⚠ FOLLOW IMPORTS. The seeds above are ENTRYPOINTS; the spawn that flashes
    # is usually a few frames down. version_drift._pid_is_alive shells out to
    # `tasklist` (console-subsystem) on every liveness probe and was missed for
    # exactly this reason -- it is imported by scheduled paths, never scheduled
    # itself, so a guard that only walks the ten scheduled scripts reported
    # green while a window flashed every few minutes on the user's desktop
    # (reported 2026-09-19). m3_core/locking.py had the identical call WITH the
    # flag, which is what made the omission a drift rather than a gap.
    #
    # Transitive over bin/-local imports only: stdlib and third-party modules
    # are not ours to police, and the traversal must terminate.
    seen: set[str] = set()
    stack = list(files)
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        for mod in _local_imports(path):
            target = _resolve_bin_module(mod)
            if target and target not in seen:
                stack.append(target)

    return sorted(seen)


def _local_imports(path: str) -> set:
    """Top-level module names imported by `path` (absolute imports only)."""
    try:
        # `with`, not a bare open(): an unclosed handle surfaces later as a
        # ResourceWarning that pytest raises as PytestUnraisableExceptionWarning
        # against whichever test happens to run next, which reads as an
        # unrelated failure.
        with open(path, encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError):
        return set()
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            out.add(node.module.split(".")[0])
    return out


def _resolve_bin_module(mod: str) -> "str | None":
    """Resolve a module name to a file under bin/, or None if it is not ours."""
    for cand in (os.path.join(_BIN, mod + ".py"),
                 os.path.join(_BIN, mod, "__init__.py")):
        if os.path.exists(cand):
            return os.path.abspath(cand)
    return None


def _argv0_literal(call: ast.Call) -> str | None:
    """Best-effort: the literal argv[0] of a spawn, if the first arg is a list
    whose first element is a string constant. Used only to skip POSIX-only cmds."""
    if not call.args:
        return None
    first = call.args[0]
    if isinstance(first, ast.List) and first.elts:
        head = first.elts[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value
    return None


def _call_has_no_window(call: ast.Call) -> bool:
    """True if the spawn passes creationflags= OR splats a no-window helper."""
    for kw in call.keywords:
        # creationflags=... (kw.arg is None for **splat)
        if kw.arg == _NOWINDOW_KWARG:
            return True
        # **no_window_kwargs() / **_no_window() / **_nw
        if kw.arg is None:
            val = kw.value
            if isinstance(val, ast.Call) and isinstance(val.func, ast.Name) \
                    and val.func.id in _NOWINDOW_HELPERS:
                return True
            if isinstance(val, ast.Name) and val.id in _NOWINDOW_HELPERS:
                return True
    return False


# Names that a spawn attribute must be called ON to count — avoids matching
# asyncio.run / loop.run_* / any unrelated .run(). We only police the subprocess
# module (imported as `subprocess` or aliased `_sp`) and the from-import forms.
_SPAWN_OBJECTS = {"subprocess", "_sp", "sp"}


def _bare_spawn_names(tree: ast.Module) -> set:
    """Names this module actually imported FROM subprocess.

    ⚠ A BARE `run(...)` IS NOT EVIDENCE OF A SPAWN. `run` is an ordinary verb:
    chatlog_prune.py defines its own `run(db, args)` and calls it from main(),
    and the guard reported it as an unflagged console spawn in a file that never
    imports subprocess at all. §3 counts that false alarm as a violation in its
    own right — a guard that cries wolf gets muted, and then it is not a guard.

    So resolve the name against the module's own imports instead of assuming.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                if alias.name in _SPAWN_ATTRS:
                    names.add(alias.asname or alias.name)
    return names


def _is_spawn(call: ast.Call, bare_names: set = frozenset()) -> bool:
    """Only real subprocess spawns:  subprocess.run(...) / _sp.Popen(...), or a
    bare run/Popen(...) that this module imported `from subprocess`. Crucially
    NOT asyncio.run / loop.run_* / <other>.run / a locally-defined run()."""
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr in _SPAWN_ATTRS:
        obj = func.value
        return isinstance(obj, ast.Name) and obj.id in _SPAWN_OBJECTS
    if isinstance(func, ast.Name) and func.id in bare_names:
        return True  # bare Popen(...) from `from subprocess import Popen`
    return False


def _offending_spawns(path: str) -> list[tuple[int, str]]:
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=path)
    bad: list[tuple[int, str]] = []
    bare = _bare_spawn_names(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_spawn(node, bare):
            continue
        cmd0 = _argv0_literal(node)
        if cmd0 and cmd0 in _POSIX_ONLY_CMDS:
            continue  # never a Windows console app → can't flash
        if not _call_has_no_window(node):
            bad.append((node.lineno, cmd0 or "<dynamic>"))
    return bad


@pytest.mark.parametrize("path", _unattended_files(),
                         ids=lambda p: os.path.relpath(p, _BIN))
def test_unattended_spawns_are_windowless(path):
    """Every subprocess spawn in an unattended entrypoint must suppress the
    console window (creationflags / no_window helper), or be a POSIX-only cmd."""
    bad = _offending_spawns(path)
    assert not bad, (
        f"{os.path.relpath(path, _BIN)} has subprocess spawn(s) without a "
        f"no-window flag (would flash a console window on Windows for every "
        f"user): lines {[ln for ln, _ in bad]} (cmds: {[c for _, c in bad]}). "
        f"Add creationflags=... or **no_window_kwargs()."
    )


def test_guard_actually_finds_the_helper_targets():
    """Sanity: the derived file set is non-empty and includes the known ones,
    so the guard can't silently pass by scanning nothing."""
    files = {os.path.basename(p) for p in _unattended_files()}
    assert files, "no unattended files derived — guard would be a no-op"
    for expected in ("m3_sdk.py", "thermal_utils.py"):
        assert expected in files, f"{expected} missing from guard scope"
