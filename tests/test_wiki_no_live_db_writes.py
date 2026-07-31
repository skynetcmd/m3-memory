"""No wiki test may reach the real engine database.

`compile_cluster` -> `_write_provenance_edges` falls back to the real
`memory.write.memory_link_impl` when `_link` is not injected. That opens the
DEFAULT engine DB — the developer's live ``~/.m3/engine/agent_memory.db`` — and
inserts provenance rows into it. Where m3 is actually running, the write instead
blocks forever on the SQLite lock held by the MCP server / dashboard /
cognitive-loop daemon, so the suite HANGS with no timeout of its own and looks
stuck rather than failing.

Two test helpers shipped with that hole (test_wiki_admission.py and
test_wiki_compile_orchestration.py). Both faked `_write`, `_supersede` and
`_ensure_prompt` but missed `_link` — an easy omission, because the fallback is
silent and CI never notices (no live engine DB there).

This module is the guard: it fails if the fallback is ever reachable again,
rather than waiting for someone to notice a hung suite or dirty store.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

_TESTS = Path(__file__).parent


def _helpers_calling(fn_name: str) -> list[tuple[Path, ast.AST]]:
    """Every `async def _run(...)` in tests/ whose body calls `fn_name`."""
    found = []
    for path in sorted(_TESTS.glob("test_wiki*.py")):
        if path.name == Path(__file__).name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            calls = [
                c for c in ast.walk(node)
                if isinstance(c, ast.Call)
                and getattr(c.func, "attr", getattr(c.func, "id", None)) == fn_name
            ]
            if calls:
                found.append((path, node, calls))
    return found


def test_every_compile_clusters_caller_injects_link():
    """A wiki test that calls compile_clusters MUST pass _link=.

    Without it the call reaches the live engine DB. Static check rather than a
    runtime one: the failure mode is a HANG, so a test that merely runs the code
    would itself hang instead of reporting.
    """
    offenders = []
    for path, fn, calls in _helpers_calling("compile_clusters"):
        for call in calls:
            kwargs = {kw.arg for kw in call.keywords if kw.arg}
            # **kw forwarding counts: the caller may supply _link through it,
            # but only if the helper itself also names _link somewhere.
            forwards = any(kw.arg is None for kw in call.keywords)
            names_link = "_link" in path.read_text(encoding="utf-8")
            if "_link" not in kwargs and not (forwards and names_link):
                offenders.append(f"{path.name}::{fn.name} (line {call.lineno})")
    assert not offenders, (
        "these call compile_clusters without injecting _link, so they fall "
        "through to the real memory_link_impl and write to the LIVE engine "
        "database (or hang on its lock): " + ", ".join(offenders)
    )


def test_write_provenance_edges_still_has_an_injection_point():
    """The seam these tests depend on must keep existing.

    If `_link=` is ever dropped from the signature, the injections above become
    silent no-ops and every wiki test quietly starts writing to the real DB
    again — the exact regression this module exists to prevent.
    """
    import sys

    _bin = str(Path(__file__).parent.parent / "bin")
    if _bin not in sys.path:
        sys.path.insert(0, _bin)
    from wiki import compile as WC  # noqa: N812

    for fn_name in ("compile_clusters", "compile_cluster", "_write_provenance_edges"):
        fn = getattr(WC, fn_name, None)
        assert fn is not None, f"wiki.compile.{fn_name} disappeared"
        assert "_link" in inspect.signature(fn).parameters, (
            f"wiki.compile.{fn_name} lost its `_link` injection point — wiki "
            f"tests would silently fall back to the live engine database"
        )


@pytest.mark.parametrize("helper", ["_write", "_supersede", "_ensure_prompt", "_link"])
def test_all_four_collaborators_are_injectable(helper):
    """compile_clusters must accept all four fakes.

    `_link` was the odd one out for long enough to ship two hanging test files;
    pinning all four together keeps them visible as a set.
    """
    import sys

    _bin = str(Path(__file__).parent.parent / "bin")
    if _bin not in sys.path:
        sys.path.insert(0, _bin)
    from wiki import compile as WC  # noqa: N812

    assert helper in inspect.signature(WC.compile_clusters).parameters
