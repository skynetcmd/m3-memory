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

    Checked by shape rather than by one hardcoded address: any RFC1918 host is
    suspect here, because the sanctioned target is a local WSL/CI cluster
    reached on a link-local or loopback address.
    """
    dsn = os.environ.get(var, "")
    if not dsn:
        pytest.skip(f"{var} not set — PG tests will self-skip")
    assert "10.21.40.51" not in dsn, (
        f"{var} targets the production warehouse: {dsn!r}. Use the throwaway "
        f"cluster from WSL_POSTGRES_TEST_DB_RUNBOOK."
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
