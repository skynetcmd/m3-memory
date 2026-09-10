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
