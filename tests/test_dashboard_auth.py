"""Dashboard authentication -- the gate, and the default it must not break.

Two properties carry equal weight here:

  1. An exposed dashboard denies. It serves memory browsing plus
     /api/audit/{override,resolve,soft-delete,hard-delete}, so an unauthenticated
     reachable instance is a remote delete button.
  2. The historical loopback-with-no-token default STILL WORKS. Breaking every
     existing user to close a hazard they do not have would be its own §3
     failure, so that path is pinned as deliberately as the gate is.

Hermetic: token resolution is stubbed, so nothing here reads the real keyring or
vault and the tests run on a CI box that has neither.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

pytest.importorskip("fastapi", reason="dashboard needs the [dashboard] extra")

import m3_http_auth as A  # noqa: E402

_TOKEN = "dashboard-test-token-not-real-0123456789"


@pytest.fixture
def stub_token(monkeypatch):
    """Force a specific (token, state) without touching the real vault."""

    def _apply(token, state=A.TokenState.OK, host="127.0.0.1"):
        monkeypatch.setattr(A, "resolve_token", lambda *a, **k: (token, state))
        monkeypatch.setenv("M3_DASHBOARD_HOST", host)
        import dashboard_server as D

        return D

    return _apply


def _middleware(D):
    """Build the middleware directly -- its config is read at construction."""
    return D.DashboardAuthMiddleware(lambda *a: None)


def test_loopback_without_token_stays_open(stub_token):
    """The pre-existing default. If this breaks, every current user breaks."""
    D = stub_token(None, A.TokenState.MISSING, host="127.0.0.1")
    assert _middleware(D)._required is False


def test_non_loopback_without_token_denies_rather_than_serves(stub_token):
    """Fail closed: reachable + no credential must not mean "allow"."""
    D = stub_token(None, A.TokenState.MISSING, host="0.0.0.0")
    mw = _middleware(D)
    assert mw._required is True
    assert mw._verifier is None  # nothing can satisfy the check -> deny-all


def test_configured_token_engages_the_gate_even_on_loopback(stub_token):
    """Configuring a token is an opt-in that must be honored on any bind."""
    D = stub_token(_TOKEN, A.TokenState.OK, host="127.0.0.1")
    mw = _middleware(D)
    assert mw._required is True
    assert mw._verifier is not None


class TestHttpFlow:
    """Drives the real FastAPI app through TestClient."""

    @staticmethod
    def _client(D, follow_redirects=False):
        from fastapi.testclient import TestClient

        # Starlette builds the middleware stack once and caches it on the app, so
        # a config resolved during an earlier test would leak into this one.
        # Dropping the built stack forces a rebuild against the current stub.
        D.app.middleware_stack = None
        return TestClient(D.app, follow_redirects=follow_redirects)

    def test_no_credentials_is_401(self, stub_token):
        D = stub_token(_TOKEN)
        assert self._client(D).get("/api/health").status_code == 401

    def test_wrong_bearer_is_401(self, stub_token):
        D = stub_token(_TOKEN)
        r = self._client(D).get("/api/health", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_correct_bearer_is_accepted(self, stub_token):
        D = stub_token(_TOKEN)
        r = self._client(D).get("/api/health", headers={"Authorization": f"Bearer {_TOKEN}"})
        assert r.status_code == 200

    def test_query_token_is_exchanged_for_an_httponly_cookie(self, stub_token):
        """A browser cannot set a header by navigating -- this is the handoff.

        The redirect must drop the token from the URL so it does not persist in
        history, server logs, or a Referer header on outbound links.
        """
        D = stub_token(_TOKEN)
        r = self._client(D).get(f"/?token={_TOKEN}")
        assert r.status_code == 303
        cookie = r.headers.get("set-cookie", "")
        assert "m3_dash" in cookie
        assert "httponly" in cookie.lower()
        assert "samesite=strict" in cookie.lower().replace(" ", "")
        assert "token=" not in r.headers.get("location", "")

    def test_cookie_authenticates_subsequent_requests(self, stub_token):
        """Covers the HTMX partials: every hx-get/hx-post target is same-origin."""
        D = stub_token(_TOKEN)
        c = self._client(D)
        c.get(f"/?token={_TOKEN}")  # sets the cookie on the client jar
        assert c.get("/api/health").status_code == 200

    def test_wrong_query_token_does_not_set_a_cookie(self, stub_token):
        D = stub_token(_TOKEN)
        r = self._client(D).get("/?token=not-the-real-token")
        assert r.status_code == 401
        assert "set-cookie" not in {k.lower() for k in r.headers}

    def test_missing_and_wrong_credentials_are_indistinguishable(self, stub_token):
        """No oracle -- a caller must not learn whether a credential merely existed."""
        D = stub_token(_TOKEN)
        c = self._client(D)
        missing = c.get("/api/health")
        wrong = c.get("/api/health", headers={"Authorization": "Bearer nope"})
        assert missing.status_code == wrong.status_code
        assert missing.content == wrong.content

    def test_delete_route_is_gated(self, stub_token):
        """The specific reason this matters: destructive routes must not be open."""
        D = stub_token(_TOKEN)
        r = self._client(D).post("/api/audit/hard-delete/some-id")
        assert r.status_code == 401

    def test_token_generated_while_running_starts_being_enforced(self, stub_token, monkeypatch):
        """The gate must not be stale in the OPEN direction.

        Config was originally read once at middleware construction, so running
        `m3 serve --generate-token` against a live dashboard left it unprotected
        until someone restarted it -- silently, while the operator had every
        reason to think they had just secured it.
        """
        import time as _time

        D = stub_token(None, A.TokenState.MISSING, host="127.0.0.1")
        c = self._client(D)
        assert c.get("/api/health").status_code != 401  # open, as before

        monkeypatch.setattr(A, "resolve_token", lambda *a, **k: (_TOKEN, A.TokenState.OK))
        # Advance past the refresh TTL rather than sleeping through it.
        base = _time.monotonic()
        monkeypatch.setattr(
            D.time, "monotonic", lambda: base + D.DashboardAuthMiddleware._TTL_SECONDS + 1
        )
        assert c.get("/api/health").status_code == 401

    def test_open_dashboard_serves__historical_default_canary(self, stub_token):
        """Pins that the MIDDLEWARE is what gates.

        With no token on loopback the same request is NOT 401. If a refactor
        makes the gate unconditional, this goes red and names the reason;
        without it, the 401 tests above could pass while every existing user's
        dashboard silently started demanding a token.
        """
        D = stub_token(None, A.TokenState.MISSING, host="127.0.0.1")
        assert self._client(D).get("/api/health").status_code != 401
