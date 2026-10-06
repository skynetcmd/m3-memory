"""The production warehouse must be unreachable from the test suite.

⚠ WHY THIS EXISTS. `M3_CDW_PG_URL` is set in a developer's real shell and points
at the shared PostgreSQL warehouse (a live, multi-machine store). A test that
resolved it would read — or write — production data. conftest's `m3_sandbox`
fixture scrubs it from every test's environment, but nothing asserted that:
delete a name from that tuple and the protection vanishes with no test failing,
which is §3's "a declared limit with no enforcement site is a lie the tests
cannot see".

The rule for PostgreSQL testing is a THROWAWAY cluster
(see ~/.m3-private/runbooks/WSL_POSTGRES_TEST_DB_RUNBOOK.md), never the CDW.
These assertions make that rule mechanical rather than remembered.
"""
from __future__ import annotations

import os

import pytest

# Warehouse-shaped env vars. A test must never see any of them resolve.
_WAREHOUSE_VARS = (
    "M3_CDW_PG_URL",
    "M3_CDW_URL",
    "PG_URL",            # legacy name for the warehouse DSN
    "M3_SYNC_TARGET_IP",
    "SYNC_TARGET_IP",
    "M3_POSTGRES_SERVER",
    "POSTGRES_SERVER",
)

# DSNs a test IS allowed to use — these must point at a throwaway cluster.
_TEST_DSN_VARS = ("M3_PRIMARY_PG_URL", "M3_PG_URL")


@pytest.mark.parametrize("var", _WAREHOUSE_VARS)
def test_warehouse_env_is_scrubbed(var):
    """The sandbox must hide every warehouse-shaped variable."""
    value = os.environ.get(var)
    assert not value, (
        f"{var} is visible to tests ({value!r}). A test that resolves it reads "
        f"or writes the production warehouse. It belongs in conftest's "
        f"_SANDBOX_CLEAR_ENV tuple."
    )


@pytest.mark.parametrize("var", _TEST_DSN_VARS)
def test_no_test_dsn_points_at_the_warehouse(var):
    """A test DSN must name a throwaway cluster, never the shared warehouse.

    Checked by SHAPE, not against a hardcoded address. Two reasons: a literal
    host would publish an internal network address in a public repository, and
    it would only ever catch the one warehouse it names. The sanctioned target
    is a disposable local cluster — loopback, or the WSL bridge on 172.16/12 —
    so any OTHER private-range host is the thing to refuse.
    """
    import ipaddress
    import re
    from urllib.parse import urlparse

    dsn = os.environ.get(var, "")
    if not dsn:
        pytest.skip(f"{var} not set — PG tests will self-skip")

    host = urlparse(dsn).hostname or ""
    if not host or not re.fullmatch(r"[0-9.]+", host):
        return  # a hostname, not a literal address — nothing to classify
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return

    if addr.is_loopback:
        return
    # The WSL bridge the PG runbook provisions lives in 172.16/12.
    if addr in ipaddress.ip_network("172.16.0.0/12"):
        return
    assert not addr.is_private, (
        f"{var} points at {host}, a private-range host that is not loopback or "
        f"the WSL bridge — that shape is a shared/production cluster. Use the "
        f"throwaway cluster from WSL_POSTGRES_TEST_DB_RUNBOOK."
    )


def test_the_conftest_scrub_list_still_covers_the_warehouse():
    """Guard the guard: the scrub tuple must keep naming these.

    Asserting on the SOURCE OF TRUTH rather than only on the effect, so a
    deletion is caught even if some other mechanism happens to mask it in the
    current environment.
    """
    import conftest

    scrubbed = set(getattr(conftest, "_SANDBOX_CLEAR_ENV", ()) or ())
    assert scrubbed, "conftest._SANDBOX_CLEAR_ENV is missing or empty"
    missing = sorted(v for v in ("M3_CDW_PG_URL", "M3_CDW_URL", "PG_URL")
                     if v not in scrubbed)
    assert not missing, (
        f"conftest no longer scrubs {missing}; a developer's real warehouse DSN "
        f"would reach the test suite"
    )


# ── the blind spot: the sandbox cannot reach import time or setUpClass ────────

def _warehouse_reads_outside_functions(path) -> list[str]:
    """`os.environ[...]` / `.get(...)` of a warehouse var at module or class scope.

    Read INSIDE a test function, a warehouse var is already scrubbed — that is
    what `test_warehouse_env_is_scrubbed` covers. Read at module scope, in a class
    body, or in a class DECORATOR, it resolves before any function-scoped fixture
    has run, so the scrub cannot help and the assertions above cannot see it.
    """
    import ast

    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    hits: list[str] = []

    def _names_in(node) -> list[str]:
        found = []
        for sub in ast.walk(node):
            # os.environ["X"]  /  os.environ.get("X")
            target = None
            if isinstance(sub, ast.Subscript) and isinstance(sub.value, ast.Attribute):
                target = sub.value
                key = sub.slice
            elif (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "get"
                    and isinstance(sub.func.value, ast.Attribute)):
                target = sub.func.value
                key = sub.args[0] if sub.args else None
            if target is None or target.attr != "environ":
                continue
            if isinstance(key, ast.Constant) and key.value in _WAREHOUSE_VARS:
                found.append(f"{key.value} (line {sub.lineno})")
        return found

    # module scope: every top-level statement that is not a function/class body,
    # plus class bodies and class decorators, which also run at import.
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if isinstance(node, ast.ClassDef):
            for dec in node.decorator_list:
                hits += _names_in(dec)
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # a classmethod body still runs before function fixtures
                    if any(getattr(d, "id", getattr(d, "attr", "")) == "classmethod"
                           for d in stmt.decorator_list) or stmt.name in (
                               "setUpClass", "tearDownClass"):
                        hits += _names_in(stmt)
                    continue
                hits += _names_in(stmt)
            continue
        hits += _names_in(node)
    return hits


def test_no_test_reads_the_warehouse_before_the_sandbox_runs():
    """The gap that let two files connect to the production warehouse.

    Measured 2026-09-29: `test_date_bound_parity.py` and `test_lease_parity.py`
    gated their PG classes on `skipUnless(os.environ.get("M3_PRIMARY_PG_URL") or
    os.environ.get("M3_CDW_PG_URL"))` and then resolved `cls.url` the same way in
    `setUpClass`. On a box where M3_CDW_PG_URL is exported and no primary DSN is
    set, that CONNECTED TO THE WAREHOUSE — 10 tests, ~101s of connect timeouts.

    Every assertion above ran green throughout, because all of them execute
    inside a test body where the sandbox has already scrubbed the variable. A
    class decorator is evaluated at IMPORT and `setUpClass` runs BEFORE
    function-scoped fixtures, so neither is reachable that way. This check reads
    the SOURCE instead, which is the only place the difference is visible.

    Use `conftest.pg_dsn()` (primary DSNs only) plus the `requires_pg` marker,
    which probes reachability once per session and auto-skips.
    """
    from pathlib import Path

    tests_dir = Path(__file__).resolve().parent
    # conftest owns the scrub list, and this file names the vars deliberately.
    exempt = {"conftest.py", Path(__file__).name}
    offenders: dict[str, list[str]] = {}
    for path in sorted(tests_dir.glob("test_*.py")):
        if path.name in exempt:
            continue
        found = _warehouse_reads_outside_functions(path)
        if found:
            offenders[path.name] = found

    assert not offenders, (
        "warehouse env var read where the sandbox cannot scrub it (module scope, "
        f"class body, class decorator, or setUpClass): {offenders}. "
        "Use conftest.pg_dsn() + @pytest.mark.requires_pg instead."
    )


def test_the_warehouse_dsn_does_not_resolve():
    """The resolver also reads the keyring / Keychain / vault, which the env
    scrub does not reach; conftest blocks that path too."""
    import sync_all
    from m3_sdk import resolve_warehouse_dsn

    assert resolve_warehouse_dsn() is None
    assert sync_all.warehouse_target() is None
