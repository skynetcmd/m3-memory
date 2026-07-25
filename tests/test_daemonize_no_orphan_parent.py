"""Nothing may stand between the daemonize Popen and os._exit(0).

The parent spawns a detached child and must hard-exit. Any teardown in between
-- a close() of a handle duplicated into that child, a print() to a dead
pythonw stdout -- can raise or block and leave the parent running the loop
alongside its child. The file's own comments record the cost: "Two live loops
each dispatch their own Semaphore(2) of SLM calls = over-dispatch that storms
the local LLM."

NOTE on the "two pythonw processes" symptom: that pair is NOT this bug. A venv's
Scripts/python[w].exe on Windows is a redirector stub that launches the base
interpreter as a child and waits, so every venv launch shows two processes. See
m3_halt._is_venv_launcher_stub. These tests guard the exit path itself, which is
worth keeping regardless.
"""

import ast
import os

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOOP = os.path.join(REPO, "bin", "m3_cognitive_loop.py")


def _daemonize_fn():
    with open(LOOP, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "daemonize_windows":
            return node
    raise AssertionError("daemonize_windows not found")


def test_popen_is_not_inside_a_with_block():
    """A context manager closes child_out before the exit, and that close hangs."""
    fn = _daemonize_fn()
    for node in ast.walk(fn):
        if isinstance(node, ast.With):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    f = inner.func
                    name = getattr(f, "attr", None) or getattr(f, "id", None)
                    if name == "Popen":
                        raise AssertionError(
                            "Popen is inside a `with` block: closing the "
                            "duplicated child stdout handle blocks on Windows "
                            "and os._exit(0) becomes unreachable"
                        )


def test_function_ends_in_os_exit():
    """Nothing may fall through past the exit."""
    fn = _daemonize_fn()
    last = fn.body[-1]
    assert isinstance(last, ast.Expr) and isinstance(last.value, ast.Call), last
    call = last.value
    assert getattr(call.func, "attr", None) == "_exit", ast.dump(call)
    assert call.args and getattr(call.args[0], "value", None) == 0


def test_spawn_failure_also_exits_rather_than_falling_through():
    """If Popen raises there is no child; continuing would run the loop from a
    console-less pythonw with dead std handles."""
    fn = _daemonize_fn()
    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "no exception handling around the spawn"
    exits = [
        n for h in handlers for n in ast.walk(h)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", None) == "_exit"
    ]
    assert exits, "a failed spawn must os._exit, not fall through to the loop"


def test_child_argv_drops_background_flag():
    """The child must not re-daemonize -- that would fork forever."""
    with open(LOOP, encoding="utf-8") as fh:
        src = fh.read()
    start = src.index("def daemonize_windows")
    body = src[start:start + 2000]
    assert '!= "--background"' in body, (
        "child argv must strip --background or the child daemonizes again"
    )
