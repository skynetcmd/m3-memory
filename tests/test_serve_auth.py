"""Bearer-token auth for the HTTP transport -- verifier, preflight, host allowlist.

The property under test is not "401 appears somewhere". It is that the server
CANNOT be started or left in a state where the HTTP surface serves the tool
catalog without a token. Every test here is chosen because it fails if that
guarantee regresses, rather than because it exercises a line.

Hermetic (§3): no network, no live server, no OS keyring, no vault. Secret
resolution is stubbed; nothing here reads or writes real credentials.
"""
from __future__ import annotations

import pathlib
import re
import sys
import unittest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

import m3_http_auth as A  # noqa: E402


class TestStaticTokenVerifier(unittest.IsolatedAsyncioTestCase):
    """The comparison itself -- the one place a credential is checked."""

    def setUp(self):
        self.token = A.generate_token()
        self.v = A.StaticTokenVerifier(self.token)

    def test_correct_token_matches(self):
        self.assertTrue(self.v.matches(self.token))

    def test_wrong_token_rejected(self):
        self.assertFalse(self.v.matches("not-the-token-not-the-token-not-it"))

    def test_empty_and_none_rejected(self):
        """A falsy credential must never be treated as "no check required"."""
        self.assertFalse(self.v.matches(""))
        self.assertFalse(self.v.matches(None))

    def test_prefix_of_real_token_rejected(self):
        """Guards against a truncating/startswith-style comparison."""
        self.assertFalse(self.v.matches(self.token[:-1]))

    def test_non_ascii_token_does_not_raise(self):
        """hmac.compare_digest raises TypeError on mixed str/bytes with non-ASCII.

        A TypeError escaping into the ASGI stack would surface as a 500 rather
        than a clean 401 -- an unauthenticated caller must not be able to pick
        the failure mode.
        """
        self.assertFalse(self.v.matches("tokén-with-ünicode-characters-here"))

    def test_cannot_construct_without_a_token(self):
        """Fail-closed is a property of the TYPE, not a branch to invert later."""
        with self.assertRaises(A.AuthConfigError):
            A.StaticTokenVerifier("")

    async def test_verify_token_returns_access_token(self):
        at = await self.v.verify_token(self.token)
        self.assertIsNotNone(at)
        self.assertEqual(at.client_id, "m3-serve")
        self.assertEqual(at.scopes, [])

    async def test_verify_token_returns_none_on_mismatch(self):
        self.assertIsNone(await self.v.verify_token("wrong-token-wrong-token-wrong-tok"))


class TestGenerateToken(unittest.TestCase):
    def test_long_enough_for_the_floor_and_for_log_redaction(self):
        """>= TOKEN_MIN_LEN, and >= 20 so chatlog_redaction's Bearer rule scrubs it."""
        tok = A.generate_token()
        self.assertGreaterEqual(len(tok), A.TOKEN_MIN_LEN)
        self.assertGreaterEqual(len(tok), 20)

    def test_tokens_are_unique(self):
        self.assertNotEqual(A.generate_token(), A.generate_token())


class TestPreflight(unittest.TestCase):
    """Startup refusal. Each state must name the condition that actually fired."""

    def test_missing_token_refuses_on_loopback(self):
        """Loopback is NOT exempt: tunnels bind loopback and publish it."""
        with self.assertRaises(A.AuthConfigError):
            A.preflight("127.0.0.1", None, A.TokenState.MISSING)

    def test_missing_token_refuses_on_public_bind(self):
        with self.assertRaises(A.AuthConfigError):
            A.preflight("0.0.0.0", None, A.TokenState.MISSING)

    def test_missing_token_message_points_at_generate(self):
        with self.assertRaises(A.AuthConfigError) as ctx:
            A.preflight("0.0.0.0", None, A.TokenState.MISSING)
        self.assertIn("--generate-token", str(ctx.exception))

    def test_short_token_refuses(self):
        with self.assertRaises(A.AuthConfigError):
            A.preflight("127.0.0.1", "tiny", A.TokenState.OK)

    def test_undecryptable_does_not_recommend_generating_a_token(self):
        """The whole reason TokenState exists.

        A secret that EXISTS but cannot be decrypted on this device must not be
        reported as "missing" -- following that advice mints a second token,
        replicates it, and can clobber the working one.

        Deliberately NOT a substring check for "--generate-token": the message
        legitimately contains that string inside a "Do NOT run ..." warning, so
        `in` would pass for the wrong reason and would keep passing if someone
        later turned the warning into a recommendation. Assert on the WARNING
        form, and that the real remedy is named.
        """
        with self.assertRaises(A.AuthConfigError) as ctx:
            A.preflight("127.0.0.1", None, A.TokenState.PRESENT_UNDECRYPTABLE)
        msg = str(ctx.exception)

        self.assertRegex(msg, r"Do NOT run `m3 serve --generate-token`")
        # No occurrence of the flag OUTSIDE that warning clause.
        self.assertIsNone(
            re.search(r"(?<!Do NOT run `m3 serve )--generate-token", msg),
            "undecryptable message must not recommend --generate-token",
        )
        # The actual remedy is the per-device salt / master key.
        self.assertIn("M3_AGENT_OS_SALT_HEX", msg)

    def test_valid_token_returned_unchanged(self):
        tok = A.generate_token()
        self.assertEqual(A.preflight("0.0.0.0", tok, A.TokenState.OK), tok)


class TestHostPatternExpansion(unittest.TestCase):
    """A7b: the MCP host validator matches exact strings or `base:*` prefixes only.

    A bare hostname does NOT match a Host header carrying a port -- which is
    exactly what a TLS-terminating tunnel sends. Getting this wrong yields a 421
    that reads as a broken tunnel rather than an allowlist rejection.
    """

    def test_bare_hostname_expands_to_both_forms(self):
        out = A.expand_host_patterns(["m3.example.com"])
        self.assertIn("m3.example.com", out)
        self.assertIn("m3.example.com:*", out)

    def test_host_with_port_also_yields_wildcard(self):
        out = A.expand_host_patterns(["m3.example.com:443"])
        self.assertIn("m3.example.com:443", out)
        self.assertIn("m3.example.com:*", out)

    def test_existing_wildcard_passed_through_once(self):
        self.assertEqual(A.expand_host_patterns(["m3.example.com:*"]), ["m3.example.com:*"])

    def test_empty_entries_ignored(self):
        self.assertEqual(A.expand_host_patterns(["", "   ", None]), [])

    def test_public_host_reaches_the_allowlist(self):
        ts = A.build_transport_security("0.0.0.0", ["m3.example.com"])
        self.assertIn("m3.example.com", ts.allowed_hosts)
        self.assertIn("m3.example.com:*", ts.allowed_hosts)

    def test_loopback_preserved_so_local_clients_keep_working(self):
        ts = A.build_transport_security("0.0.0.0", ["m3.example.com"])
        self.assertIn("127.0.0.1:*", ts.allowed_hosts)


class TestBuildAuth(unittest.TestCase):
    def test_does_not_advertise_an_oauth_authorization_server(self):
        """resource_server_url stays None on purpose.

        Setting it mounts /.well-known/oauth-protected-resource, advertising an
        authorization server we do not run and inviting clients into a Dynamic
        Client Registration flow that cannot succeed.
        """
        settings, _ = A.build_auth("127.0.0.1", 8080, A.generate_token())
        self.assertIsNone(settings.resource_server_url)
        self.assertEqual(settings.required_scopes, [])

    def test_returns_a_verifier_bound_to_the_token(self):
        tok = A.generate_token()
        _, verifier = A.build_auth("127.0.0.1", 8080, tok)
        self.assertTrue(verifier.matches(tok))
        self.assertFalse(verifier.matches(A.generate_token()))


class TestAttachGuard(unittest.TestCase):
    """`attach` must refuse rather than half-wire a live instance."""

    def _fastmcp(self):
        from mcp.server.fastmcp import FastMCP

        return FastMCP("test-attach")

    def test_attach_sets_both_fields_on_the_real_instance(self):
        mcp = self._fastmcp()
        tok = A.generate_token()
        verifier = A.attach(mcp, "127.0.0.1", 8080, tok)
        self.assertIs(mcp._token_verifier, verifier)
        self.assertIsNotNone(mcp.settings.auth)

    def test_refuses_when_the_private_attribute_is_gone(self):
        """A rename upstream must be a loud refusal, not a new unread attribute.

        Checked against the object actually being mutated -- a throwaway probe
        can be healthy while the instance in hand is not.
        """

        class Renamed:
            settings = type("S", (), {"auth": None, "transport_security": None})()

        with self.assertRaises(A.AuthConfigError) as ctx:
            A.attach(Renamed(), "127.0.0.1", 8080, A.generate_token())
        self.assertIn("refusing to start unauthenticated", str(ctx.exception))

    def test_refuses_when_settings_has_no_auth_field(self):
        class NoAuthField:
            _token_verifier = None
            settings = type("S", (), {})()

        with self.assertRaises(A.AuthConfigError):
            A.attach(NoAuthField(), "127.0.0.1", 8080, A.generate_token())

    def test_public_host_reaches_transport_security(self):
        mcp = self._fastmcp()
        A.attach(mcp, "127.0.0.1", 8080, A.generate_token(), ["m3.example.com"])
        hosts = mcp.settings.transport_security.allowed_hosts
        self.assertIn("m3.example.com", hosts)
        self.assertIn("m3.example.com:*", hosts)


class TestHttpEnforcement(unittest.TestCase):
    """The end-to-end property: the real ASGI app refuses unauthenticated calls.

    Drives the actual Starlette app FastMCP builds -- not a hand-rolled stand-in
    -- because the thing worth pinning is that the middleware is really in the
    request path, which only the real app can demonstrate.
    """

    _HEADERS = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    _INIT = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
    }

    # One FastMCP + one lifespan for the whole class. Each `streamable_http_app()`
    # spins a StreamableHTTP session manager whose memory streams are closed by
    # the app's lifespan, so a per-test app that is entered and exited repeatedly
    # leaks streams -- which this repo's `-W error` config correctly turns red.
    # Policy is to fix such leaks at the source rather than filter them, so the
    # app is built once and its lifespan entered once.
    @classmethod
    def setUpClass(cls):
        from mcp.server.fastmcp import FastMCP
        from starlette.testclient import TestClient

        cls.token = A.generate_token()
        mcp = FastMCP("test-http")
        # "testserver" is the Host TestClient sends; without it the transport's
        # DNS-rebinding guard 421s before auth is ever consulted -- which is the
        # tunnel footgun this allowlist exists to fix.
        A.attach(mcp, "127.0.0.1", 8080, cls.token, ["testserver"])
        cls._ctx = TestClient(mcp.streamable_http_app())
        cls.client = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def _post(self, token: "str | None"):
        headers = dict(self._HEADERS)
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return self.client.post("/mcp", json=self._INIT, headers=headers)

    def test_no_authorization_header_is_401(self):
        self.assertEqual(self._post(None).status_code, 401)

    def test_wrong_token_is_401(self):
        self.assertEqual(self._post(A.generate_token()).status_code, 401)

    def test_wrong_token_and_missing_token_are_indistinguishable(self):
        """No oracle: a caller must not learn whether a token merely existed."""
        missing = self._post(None)
        wrong = self._post(A.generate_token())
        self.assertEqual(missing.status_code, wrong.status_code)
        self.assertEqual(missing.content, wrong.content)

    def test_correct_token_is_not_rejected(self):
        r = self._post(self.token)
        self.assertNotEqual(r.status_code, 401)
        self.assertNotEqual(r.status_code, 403)


class TestForeignHostRejection(unittest.TestCase):
    """Footgun A, pinned: an unlisted Host 421s BEFORE auth runs.

    This is why `--public-host` exists. Without it a tunnel forwarding its own
    public Host header fails in a way that reads as a broken tunnel rather than
    an allowlist rejection. Separate class so it gets its own app + lifespan
    (see the leak note in TestHttpEnforcement).
    """

    def test_foreign_host_is_rejected_when_not_allowlisted(self):
        from mcp.server.fastmcp import FastMCP
        from starlette.testclient import TestClient

        token = A.generate_token()
        mcp = FastMCP("test-host")
        A.attach(mcp, "127.0.0.1", 8080, token)  # no public_hosts -> loopback only
        with TestClient(mcp.streamable_http_app()) as c:
            r = c.post(
                "/mcp",
                json=TestHttpEnforcement._INIT,
                headers={**TestHttpEnforcement._HEADERS, "Authorization": f"Bearer {token}"},
            )
        self.assertEqual(r.status_code, 421)


class TestFailOpenCanary(unittest.TestCase):
    """§11: pins that the MIDDLEWARE is what gates, not something ambient.

    Builds the same app WITHOUT attach() and confirms an unauthenticated call is
    NOT 401. If a refactor drops the wiring, this goes red alongside the 401
    tests instead of them silently passing for the wrong reason.
    """

    def test_app_without_attach_does_not_401(self):
        from mcp.server.fastmcp import FastMCP
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.testclient import TestClient

        mcp = FastMCP("test-open")
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
        with TestClient(mcp.streamable_http_app()) as c:
            r = c.post(
                "/mcp",
                json=TestHttpEnforcement._INIT,
                headers=TestHttpEnforcement._HEADERS,
            )
        self.assertNotEqual(r.status_code, 401)


class TestPortability(unittest.TestCase):
    """The support matrix is 3 OSes x 2 DBs x Python 3.11-3.14.

    These pin the properties that would otherwise only surface on a platform
    nobody develops on, and they are cheap enough to be worth stating outright.
    """

    def test_no_platform_specific_imports(self):
        """Auth must not grow an OS-specific dependency.

        Secret storage already handles per-OS keyrings behind auth_utils; this
        module reaching for `winreg`, `pwd`, or a `security`/`cmdkey` subprocess
        would fork that seam and break the other two platforms silently.
        """
        import ast

        src = (_BIN / "m3_http_auth.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for banned in ("winreg", "pwd", "grp", "msvcrt", "fcntl", "subprocess"):
            self.assertNotIn(banned, imported, f"{banned} is not portable across the matrix")

    def test_vault_probe_is_dialect_parameterized(self):
        """A literal '?' placeholder is a portability bug on PostgreSQL.

        The probe must ask auth_utils for the active dialect's placeholder rather
        than hardcoding SQLite's, or it raises a syntax error on PG -- which the
        probe would swallow, silently collapsing PRESENT_UNDECRYPTABLE into
        MISSING and pointing the operator at the wrong remedy.
        """
        src = (_BIN / "m3_http_auth.py").read_text(encoding="utf-8")
        probe = src[src.index("def _vault_row_exists") : src.index("def resolve_token")]
        self.assertIn("_dialect_param()", probe)
        self.assertNotIn("service_name = ?", probe)

    def test_no_os_path_gate_on_the_vault(self):
        """On PostgreSQL the vault has no FILE.

        auth_utils documents this: an os.path.exists gate above the vault read
        makes every secret silently resolve to None on PG. The probe must not
        reintroduce one.

        Parses the AST rather than substring-matching the source: the function's
        own docstring says "no os.path.exists gate", and a text search cannot
        tell CODE from PROSE ABOUT CODE -- it failed on exactly that comment
        first time round. Same trap as the repo's drift guard that tripped over a
        docstring warning against an import.
        """
        import ast

        src = (_BIN / "m3_http_auth.py").read_text(encoding="utf-8")
        fn = next(
            n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.FunctionDef) and n.name == "_vault_row_exists"
        )
        calls = {
            ast.unparse(n.func)
            for n in ast.walk(fn)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        self.assertNotIn("os.path.exists", calls)
        self.assertNotIn("pathlib.Path.exists", calls)

    def test_token_charset_is_url_and_header_safe(self):
        """token_urlsafe output must survive a URL query param and an HTTP header.

        The dashboard hands the token off as `?token=...`, so a character needing
        percent-encoding would arrive altered and fail the comparison.
        """
        import string

        allowed = set(string.ascii_letters + string.digits + "-_")
        for _ in range(20):
            self.assertTrue(set(A.generate_token()) <= allowed)


class TestBridgeWiring(unittest.TestCase):
    """The bridge's http branch, asserted at the source rather than by running it.

    The transport block lives under `if __name__ == "__main__":`, so it cannot be
    imported and called. These read the source instead -- coarse, but they pin the
    properties that would otherwise regress silently: that the refusal EXITS, and
    that no code path reaches `mcp.run` after an AuthConfigError.
    """

    @classmethod
    def setUpClass(cls):
        cls.src = (_BIN / "memory_bridge.py").read_text(encoding="utf-8")
        start = cls.src.index('if transport in ("http"')
        end = cls.src.index("        mcp.run(transport=", start)
        cls.http_block = cls.src[start:end]

    def test_http_branch_preflights_before_running(self):
        self.assertIn("preflight(", self.http_block)
        self.assertIn("attach(", self.http_block)

    def test_refusal_exits_nonzero_rather_than_falling_through(self):
        """A caught AuthConfigError must terminate, not continue to mcp.run()."""
        self.assertIn("AuthConfigError", self.http_block)
        self.assertIn("sys.exit(1)", self.http_block)

    def test_exit_uses_a_name_that_exists_in_scope(self):
        """`_sys.exit` would NameError -- i.e. the fail-closed path would crash.

        The bridge aliases `import os as _os` inside __main__ but has no `_sys`,
        so this pins the one that is actually bound.
        """
        self.assertNotIn("_sys.exit", self.http_block)

    def test_stdio_branch_does_not_mention_auth(self):
        """§4/§12: the default path runs zero new code -- no lookup, no warning."""
        stdio = self.src[self.src.index('logger.info("Transport: stdio")') :]
        for token in ("m3_http_auth", "preflight", "resolve_token", "AuthConfigError"):
            self.assertNotIn(token, stdio)


class TestCliSurface(unittest.TestCase):
    """`m3 serve` flags and their refusal paths.

    Stubs the auth module's resolution + storage so nothing here touches the real
    keyring or vault -- these must be runnable on a CI box that has neither.
    """

    def setUp(self):
        from m3_memory import cli

        self.cli = cli
        self._real_import = cli._import_http_auth
        cli._import_http_auth = lambda: A
        self._real_resolve = A.resolve_token
        self.addCleanup(self._restore)

    def _restore(self):
        self.cli._import_http_auth = self._real_import
        A.resolve_token = self._real_resolve

    def _args(self, **kw):
        import argparse

        base = dict(
            host="127.0.0.1", port=8080, path="/mcp", public_host=None,
            generate_token=False, show_token=False, force=False,
        )
        base.update(kw)
        return argparse.Namespace(**base)

    def test_serve_refuses_without_a_token(self):
        """The property that matters: no token -> nonzero exit, bridge never runs."""
        A.resolve_token = lambda *a, **k: (None, A.TokenState.MISSING)
        ran = []
        real_run = self.cli._run_bridge
        self.cli._run_bridge = lambda: ran.append(True)
        try:
            rc = self.cli._cmd_serve(self._args())
        finally:
            self.cli._run_bridge = real_run
        self.assertEqual(rc, 1)
        self.assertEqual(ran, [], "_run_bridge must not be reached without a token")

    def test_serve_starts_when_a_token_is_present(self):
        A.resolve_token = lambda *a, **k: (A.generate_token(), A.TokenState.OK)
        ran = []
        real_run = self.cli._run_bridge
        self.cli._run_bridge = lambda: ran.append(True)
        try:
            rc = self.cli._cmd_serve(self._args())
        finally:
            self.cli._run_bridge = real_run
        self.assertEqual(rc, 0)
        self.assertEqual(ran, [True])

    def test_public_host_is_passed_to_the_bridge_env(self):
        """--public-host must reach the bridge, or tunnels 421 before auth."""
        import os

        A.resolve_token = lambda *a, **k: (A.generate_token(), A.TokenState.OK)
        real_run = self.cli._run_bridge
        self.cli._run_bridge = lambda: None
        prior = os.environ.get("M3_HTTP_PUBLIC_HOST")
        try:
            self.cli._cmd_serve(self._args(public_host=["a.example.com", "b.example.com"]))
            self.assertEqual(os.environ["M3_HTTP_PUBLIC_HOST"], "a.example.com,b.example.com")
        finally:
            self.cli._run_bridge = real_run
            if prior is None:
                os.environ.pop("M3_HTTP_PUBLIC_HOST", None)
            else:
                os.environ["M3_HTTP_PUBLIC_HOST"] = prior

    def test_generate_token_refuses_to_clobber_without_force(self):
        """Rotation invalidates live connectors, and the row replicates by version."""
        A.resolve_token = lambda *a, **k: ("an-existing-token-value-0123456789", A.TokenState.OK)
        self.assertEqual(self.cli._cmd_serve(self._args(generate_token=True)), 1)

    def test_generate_token_refuses_on_undecryptable_without_force(self):
        """The worst clobber case: replicate a new token over one that still works."""
        A.resolve_token = lambda *a, **k: (None, A.TokenState.PRESENT_UNDECRYPTABLE)
        self.assertEqual(self.cli._cmd_serve(self._args(generate_token=True)), 1)

    def test_show_token_never_prints_the_secret(self):
        import contextlib
        import io

        secret = "super-secret-token-value-0123456789abc"
        A.resolve_token = lambda *a, **k: (secret, A.TokenState.OK)
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = self.cli._cmd_serve(self._args(show_token=True))
        self.assertEqual(rc, 0)
        self.assertNotIn(secret, buf.getvalue() + err.getvalue())

    def test_show_token_reports_missing_with_nonzero_exit(self):
        import contextlib
        import io

        A.resolve_token = lambda *a, **k: (None, A.TokenState.MISSING)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.cli._cmd_serve(self._args(show_token=True)), 1)


class TestResolveTokenStates(unittest.TestCase):
    """resolve_token's three-way outcome, with the secret seam stubbed."""

    def _run_with(self, secret, row_exists):
        import m3_sdk

        real_secret, real_probe = m3_sdk.get_secret, A._vault_row_exists
        m3_sdk.get_secret = lambda _s: secret
        A._vault_row_exists = lambda _s=A.SECRET_NAME: row_exists
        try:
            return A.resolve_token()
        finally:
            m3_sdk.get_secret, A._vault_row_exists = real_secret, real_probe

    def test_present_and_decryptable_is_ok(self):
        tok, state = self._run_with("a-real-looking-token-value-here-ok", False)
        self.assertEqual(state, A.TokenState.OK)
        self.assertEqual(tok, "a-real-looking-token-value-here-ok")

    def test_absent_everywhere_is_missing(self):
        tok, state = self._run_with(None, False)
        self.assertEqual(state, A.TokenState.MISSING)
        self.assertIsNone(tok)

    def test_row_exists_but_undecryptable_is_distinguished(self):
        """The MISSING/UNDECRYPTABLE split that makes the refusal safe to act on."""
        tok, state = self._run_with(None, True)
        self.assertEqual(state, A.TokenState.PRESENT_UNDECRYPTABLE)
        self.assertIsNone(tok)

    def test_whitespace_only_secret_is_not_a_token(self):
        _, state = self._run_with("   ", False)
        self.assertEqual(state, A.TokenState.MISSING)

    def test_vault_probe_failure_degrades_to_missing(self):
        """A probe is a diagnosis aid; it must never crash startup."""
        import m3_sdk

        real_secret = m3_sdk.get_secret
        real_backend = A.__dict__.get("_vault_row_exists")
        m3_sdk.get_secret = lambda _s: None
        try:
            # Real _vault_row_exists against a broken seam -> False, not an exception.
            self.assertFalse(A._vault_row_exists("definitely-not-a-real-service-name"))
        finally:
            m3_sdk.get_secret = real_secret
            if real_backend is not None:
                A._vault_row_exists = real_backend


if __name__ == "__main__":
    unittest.main()
