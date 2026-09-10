"""Live-PG coverage for the serve-auth vault probe.

`m3_http_auth._vault_row_exists` is the only part of the auth path that touches a
database, and it is what separates "no token configured" from "a token exists but
this device cannot decrypt it" -- a distinction that decides which remediation the
operator is told to run. Getting it wrong on PostgreSQL would tell someone to run
`--generate-token`, which mints a second token, replicates it by version bump, and
can orphan the one that still works elsewhere.

The SQLite path is covered in test_serve_auth.py. This proves the same predicate
on a live PG store, where the vault has no FILE at all -- the case an
`os.path.exists`-style gate silently breaks (see the comment in
auth_utils.get_api_key's vault read for the original incident).

Skips cleanly without a reachable cluster. DSN from M3_PRIMARY_PG_URL/M3_PG_URL
(never PG_URL -- that's the warehouse var).

⚠ FINDING (2026-09-09, verified on WSL PG 16 m3_test at schema_version 48):
`synchronized_secrets` DOES NOT EXIST in the PostgreSQL PRIMARY schema. It is
defined for SQLite (migrations 003_encrypted_secrets.sql,
007_synchronized_secrets.sql) and for the WAREHOUSE
(pg_warehouse_chatlog_v1.sql, as `m3_warehouse.synchronized_secrets`), but
`pg_primary_v1.sql` contains ZERO references and no numbered pg_NNN migration
adds it.

Consequences, measured rather than inferred:
  * On a PG primary the vault tier of auth_utils.get_api_key is structurally
    dead -- the query raises UndefinedTable, which that function swallows, so
    every vault-stored secret silently resolves to None. Only env/keyring work.
  * `_vault_row_exists` therefore always returns False there, so
    PRESENT_UNDECRYPTABLE can never fire on PG. It degrades to MISSING, which is
    the safe direction (the operator is told to generate a token, and on PG
    there is genuinely no vault row to clobber) -- but the diagnosis is weaker
    than on SQLite.

So these tests SKIP on a stock PG primary, and that skip is correct rather than
a gap in this file. Closing it means adding the table to the PG primary schema,
which is an m3 schema-parity change well outside auth's scope; it is recorded
here so the next reader does not re-derive it. These tests become live the
moment that parity lands.
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

pytestmark = pytest.mark.requires_pg


@pytest.fixture()
def pg_backend(monkeypatch, pg_url):
    """Point the auth module's vault reads at the live PG store.

    ⚠ Skips unless the target actually carries the m3 vault schema in its default
    search path. Two of these tests WRITE (set_api_key), and pointing a write at
    the wrong Postgres is not a test failure -- it is a mutation of somebody's
    store. The warehouse is exactly such a target: it holds synchronized_secrets
    under the `m3_warehouse` schema, so an unqualified INSERT lands nowhere while
    the connection still succeeds. Verified rather than assumed: without this
    guard these tests raised UndefinedTable *from inside set_api_key* when handed
    a warehouse DSN.
    """
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PRIMARY_PG_URL", pg_url)
    import auth_utils

    # Drop any backend cached from an earlier SQLite-backed test in this session.
    for attr in ("_BACKEND", "_backend_cache"):
        if hasattr(auth_utils, attr):
            monkeypatch.setattr(auth_utils, attr, None, raising=False)

    try:
        p = auth_utils._dialect_param()
        with auth_utils._backend().open_readonly("") as conn:
            cur = conn.cursor()
            cur.execute(f"SELECT 1 FROM synchronized_secrets WHERE service_name = {p}",
                        ("__schema_probe__",))
            cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(
            "target Postgres has no reachable synchronized_secrets table "
            f"(not an m3 primary store -- a warehouse/mirror will do this): {exc}"
        )
    return auth_utils


def test_absent_service_reports_no_row(pg_backend):
    """A name that was never written must read as MISSING, not as an error.

    The probe is best-effort by contract: if it raised, resolve_token would
    degrade every state to MISSING and the undecryptable diagnosis would be
    unreachable on PG.
    """
    import m3_http_auth as A

    assert A._vault_row_exists(f"NEVER_WRITTEN_{uuid.uuid4().hex}") is False


def test_probe_sees_a_row_written_through_the_seam(pg_backend):
    """Round-trip: write a vault row on PG, and the probe must find it.

    Uses a throwaway service name and removes it afterwards so the shared store
    is left as it was found.
    """
    import m3_http_auth as A
    from auth_utils import set_api_key

    service = f"M3_SERVE_TOKEN_TEST_{uuid.uuid4().hex[:8]}"
    try:
        set_api_key(service, "not-a-real-token-value-0123456789abcd")
    except ValueError as exc:  # no AGENT_OS_MASTER_KEY on this box
        pytest.skip(f"vault writes need a master key: {exc}")

    try:
        assert A._vault_row_exists(service) is True
    finally:
        _delete_secret(service)


def test_undecryptable_row_is_distinguished_from_missing(pg_backend, monkeypatch):
    """The state split that makes the refusal safe to act on, proven on PG.

    Simulates the cross-machine case -- a replicated row this device's salt
    cannot decrypt -- by forcing the plaintext lookup to fail while the row is
    really present in PG.
    """
    import m3_http_auth as A
    from auth_utils import set_api_key

    service = f"M3_SERVE_TOKEN_TEST_{uuid.uuid4().hex[:8]}"
    try:
        set_api_key(service, "not-a-real-token-value-0123456789abcd")
    except ValueError as exc:
        pytest.skip(f"vault writes need a master key: {exc}")

    try:
        import m3_sdk

        monkeypatch.setattr(m3_sdk, "get_secret", lambda _s: None)
        token, state = A.resolve_token(service)
        assert token is None
        assert state is A.TokenState.PRESENT_UNDECRYPTABLE

        # And the refusal must name the real remedy, not --generate-token.
        with pytest.raises(A.AuthConfigError) as ctx:
            A.preflight("127.0.0.1", token, state)
        assert "M3_AGENT_OS_SALT_HEX" in str(ctx.value)
    finally:
        _delete_secret(service)


def test_probe_degrades_to_false_when_the_table_is_absent(pg_url, monkeypatch):
    """The vault probe must not raise on a PG primary that lacks the table.

    Deliberately NOT gated on the schema fixture: this is the stock-PG case (see
    the module docstring), so it runs exactly where the others skip. If the probe
    raised instead of returning False, resolve_token would propagate and the
    bridge would refuse to start on every PostgreSQL deployment -- turning a
    weaker diagnosis into a hard outage.
    """
    monkeypatch.setenv("M3_DB_BACKEND", "postgres")
    monkeypatch.setenv("M3_PRIMARY_PG_URL", pg_url)
    import m3_http_auth as A

    assert A._vault_row_exists("ANY_SERVICE_NAME") is False
    _token, state = A.resolve_token()
    # MISSING, never an exception -- and never PRESENT_UNDECRYPTABLE, which would
    # send the operator chasing a salt problem that does not exist on PG.
    assert state in (A.TokenState.MISSING, A.TokenState.OK)


def _delete_secret(service: str) -> None:
    """Remove a test row through the seam (dialect-parameterized, PG or SQLite)."""
    import auth_utils

    try:
        p = auth_utils._dialect_param()
        with auth_utils._backend().connection() as conn:
            cur = conn.cursor()
            cur.execute(
                f"DELETE FROM synchronized_secrets WHERE service_name = {p}", (service,)
            )
            conn.commit()
    except Exception:  # noqa: BLE001 -- cleanup must not mask a real assertion
        pass
