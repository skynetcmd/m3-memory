#!/usr/bin/env python3
"""Bearer-token authentication for m3's HTTP surfaces (`m3 serve`, the dashboard).

Why this exists
---------------
`m3 serve` publishes the full tool catalog -- including ``memory_delete`` and
``gdpr_forget`` -- over HTTP. Anthropic's custom connectors dial IN from their
cloud, not from the user's device, so a connector deployment MUST be reachable
from the public internet; a tailnet/VPN-only bind cannot work. That makes an
unauthenticated bridge a publicly writable memory store, so auth has to be a
property of the server rather than something the operator assembles correctly
out of a reverse proxy on the first try.

Design notes
------------
* **Fail CLOSED, structurally (§3/§6).** ``StaticTokenVerifier`` cannot be
  constructed without a token, and it has no "allow when unset" branch to
  invert later. Compare that with the older ``mcp_proxy._check_auth``, which
  returns True when no key is configured -- a fail-OPEN default that is exactly
  wrong for a surface intended to be exposed.
* **Constant-time compare (§6).** ``hmac.compare_digest``, never ``==``.
* **The two failure directions are not symmetric (§3).** A server that wrongly
  refuses to start is an annoyance the operator sees immediately; one that
  wrongly starts unauthenticated is a public memory store nobody notices. So
  every ambiguous case resolves to REFUSE -- and each refusal names the
  condition that actually fired, because a refusal that misidentifies its cause
  sends the operator to fix the wrong thing.
* **Headless-safe secret resolution (§1).** The token comes from
  ``m3_sdk.get_secret`` (env -> keyring -> native store -> encrypted vault), so
  it works under systemd/launchd/Task Scheduler with no interactive prompt and
  no network. Do NOT read ``os.environ`` directly here; that reinvents the seam
  env-only and breaks the vault tier.
* **Shared by both surfaces (§2).** The bridge and the dashboard import this
  module rather than each growing a token comparison. Two copies is the defect
  independent of whether either copy is correct.

Portability: ``secrets`` and ``hmac`` are stdlib and behave identically across
the supported matrix (Windows/macOS/Linux, Python 3.11-3.14). The vault probe
goes through the backend seam, so it is correct on SQLite and PostgreSQL alike.
"""
from __future__ import annotations

import enum
import hmac
import logging
import secrets

logger = logging.getLogger("m3_http_auth")

# Service name for the shared bearer token. Survives auth_utils._sanitize_service
# (NFKC + [A-Za-z0-9_-]) unchanged, so what we write is what we read back.
SECRET_NAME = "M3_SERVE_TOKEN"

# 32 bytes of entropy -> ~43 urlsafe chars. Two independent reasons for the floor:
# a short token on a public bridge is fail-open by another name, and
# chatlog_redaction's `bearer_generic` rule only scrubs "Bearer <20+ chars>", so
# a shorter token would not inherit log redaction.
_TOKEN_ENTROPY_BYTES = 32
TOKEN_MIN_LEN = 32

# Identifies this credential in the AccessToken the MCP layer hands downstream.
# Static shared token: one logical client, no per-user identity to model.
_CLIENT_ID = "m3-serve"

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "::ffff:127.0.0.1"})


class AuthConfigError(RuntimeError):
    """Startup-time auth misconfiguration. Always fatal -- never degrade to no auth."""


class TokenState(enum.Enum):
    """Why ``resolve_token`` returned what it did.

    The distinction is load-bearing, not cosmetic. ``synchronized_secrets`` is
    replicated between machines by pg_sync, but the PBKDF2 salt is PER-DEVICE
    (auth_utils._get_device_salt reads a local file). So a token minted on
    machine A syncs its ciphertext to machine B, where the derived key differs
    and decryption returns None -- indistinguishable, without this probe, from
    "no token configured".

    Conflating them is actively harmful: the operator would be told to run
    ``--generate-token``, which mints a SECOND token, re-syncs it, and can
    clobber the working one by version bump. Naming the real cause is what
    makes the refusal safe to act on (§3).
    """

    OK = "ok"
    MISSING = "missing"
    PRESENT_UNDECRYPTABLE = "present_undecryptable"


class StaticTokenVerifier:
    """Verifies a single shared bearer token.

    Structurally satisfies the MCP ``TokenVerifier`` protocol (one async
    ``verify_token``) without importing it -- the protocol is runtime-checkable
    by shape, and not importing keeps this module usable by the dashboard,
    which has no MCP dependency.
    """

    def __init__(self, expected: str) -> None:
        if not expected:
            # Fail-closed as a type property: there is no such thing as a
            # verifier with no token to compare against.
            raise AuthConfigError("StaticTokenVerifier requires a non-empty token")
        # Pre-encode: compare_digest raises TypeError on mixed str/bytes once a
        # non-ASCII token is involved, and encoding per-call would repeat that
        # risk at every request.
        self._expected = expected.encode("utf-8")

    def matches(self, token: str | None) -> bool:
        """Constant-time compare. The single place a token is checked.

        Note compare_digest does not hide LENGTH differences. That is immaterial
        here: the token is a fixed-length token_urlsafe value, so length carries
        no secret. Hashing both sides first would blind it, and is deliberately
        not done -- it would add a step whose only effect is to obscure a
        non-secret.
        """
        if not token:
            return False
        return hmac.compare_digest(token.encode("utf-8"), self._expected)

    async def verify_token(self, token: str):
        """MCP TokenVerifier hook. Returns an AccessToken, or None to reject."""
        if not self.matches(token):
            return None
        from mcp.server.auth.provider import AccessToken

        return AccessToken(token=token, client_id=_CLIENT_ID, scopes=[], expires_at=None)


def generate_token() -> str:
    """Mint a new bearer token. ~43 urlsafe chars from 32 bytes of entropy."""
    return secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)


def is_loopback(host: str | None) -> bool:
    """True if `host` is a loopback bind address.

    Used only to WORD errors and to decide whether the dashboard's historical
    no-auth default still applies -- never to decide whether the bridge needs a
    token. Tunnels (cloudflared/ngrok) bind loopback and expose it publicly, so
    loopback is not evidence of safety.
    """
    return (host or "").strip().lower() in _LOOPBACK_HOSTS


def _vault_row_exists(service: str = SECRET_NAME) -> bool:
    """True if the vault holds a row for `service` (regardless of decryptability).

    Read-only, through the backend seam so it is correct on both SQLite and
    PostgreSQL. Mirrors auth_utils' own vault read: no os.path.exists gate --
    on PG there is no vault FILE, and gating on one would make this silently
    report "no row" for every PG install.

    Best-effort by contract: any failure returns False, which degrades the
    diagnosis to MISSING rather than breaking startup. A worse message is
    acceptable; a crash in a probe is not.
    """
    conn = None
    _cm = None
    try:
        import auth_utils

        _p = auth_utils._dialect_param()
        _cm = auth_utils._backend().open_readonly(auth_utils._vault_db_path())
        conn = _cm.__enter__()
        cur = conn.cursor()
        cur.execute(
            f"SELECT 1 FROM synchronized_secrets WHERE service_name = {_p}",
            (service,),
        )
        return cur.fetchone() is not None
    except Exception as exc:  # noqa: BLE001 -- diagnosis aid, never fatal
        logger.debug(f"vault probe for {service} failed: {type(exc).__name__}: {exc}")
        return False
    finally:
        if _cm is not None and conn is not None:
            try:
                _cm.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


def resolve_token(service: str = SECRET_NAME) -> "tuple[str | None, TokenState]":
    """Resolve the bearer token and explain the outcome.

    Returns ``(token, TokenState)``. A None token with PRESENT_UNDECRYPTABLE
    means a secret EXISTS but this device cannot decrypt it -- see TokenState.
    """
    from m3_sdk import get_secret

    token = (get_secret(service) or "").strip()
    if token:
        return token, TokenState.OK
    if _vault_row_exists(service):
        return None, TokenState.PRESENT_UNDECRYPTABLE
    return None, TokenState.MISSING


def preflight(host: str, token: "str | None", state: TokenState) -> str:
    """Validate startup auth config. Returns the token, or raises AuthConfigError.

    The refusal is UNCONDITIONAL -- it does not exempt loopback. A loopback
    exemption would be a silent bypass, because the tunnels this feature exists
    to support bind loopback and publish it. `host` only shapes the wording.
    """
    if state is TokenState.PRESENT_UNDECRYPTABLE:
        raise AuthConfigError(
            f"A '{SECRET_NAME}' secret exists in the vault but THIS DEVICE cannot decrypt it.\n"
            "  Cause: the encryption salt / master key is per-device, and secrets are\n"
            "         replicated between machines -- so this row was almost certainly\n"
            "         written on a different machine.\n"
            "  Fix:   restore this device's salt via M3_AGENT_OS_SALT_HEX (or its backup),\n"
            "         or confirm AGENT_OS_MASTER_KEY is present in the OS keyring.\n"
            "  Do NOT run `m3 serve --generate-token` to work around this: it mints a\n"
            "  SECOND token, replicates it, and can clobber the working one."
        )

    if state is TokenState.MISSING or not token:
        exposure = (
            "loopback, but tunnels (cloudflared/ngrok) publish loopback to the internet"
            if is_loopback(host)
            else f"'{host}' -- a NON-LOOPBACK bind, reachable beyond this machine"
        )
        raise AuthConfigError(
            f"No {SECRET_NAME} configured; refusing to start an unauthenticated HTTP bridge.\n"
            f"  Bind is {exposure}.\n"
            "  The HTTP surface exposes destructive tools (memory_delete, gdpr_forget).\n"
            "  Generate a token:  m3 serve --generate-token"
        )

    if len(token) < TOKEN_MIN_LEN:
        raise AuthConfigError(
            f"{SECRET_NAME} is {len(token)} chars; the minimum is {TOKEN_MIN_LEN}.\n"
            "  A weak token on a publicly reachable bridge is fail-open by another name.\n"
            "  Generate a strong one:  m3 serve --generate-token --force"
        )

    return token


def expand_host_patterns(hosts: "list[str] | tuple[str, ...]") -> "list[str]":
    """Expand each hostname into the forms the MCP host validator actually matches.

    ⚠ The validator (mcp.server.transport_security._validate_host) matches an
    EXACT string, or a ``base:*`` pattern by prefix. It does NOT treat a bare
    hostname as "any port" -- so 'm3.example.com' alone will NOT match a request
    carrying ``Host: m3.example.com:443``, which is precisely what a TLS-
    terminating tunnel sends.

    Emitting both forms is what keeps a tunnel from failing with a 421 that
    looks like a broken tunnel rather than a host-allowlist rejection.
    """
    out: list[str] = []
    for raw in hosts:
        h = (raw or "").strip()
        if not h:
            continue
        if h.endswith(":*"):  # already a wildcard pattern -- take as given
            if h not in out:
                out.append(h)
            continue
        base = h.rsplit(":", 1)[0] if (":" in h and not h.startswith("[")) else h
        for form in (h, f"{base}:*"):
            if form not in out:
                out.append(form)
    return out


def build_transport_security(host: str, public_hosts: "list[str] | None" = None):
    """Build the Host/Origin allowlist for the streamable-HTTP transport.

    FastMCP auto-enables DNS-rebinding protection when its CONSTRUCTION-time host
    is loopback -- which it always is in memory_bridge, because the real bind
    host is assigned to settings afterwards. The resulting allowlist is
    loopback-only, so a tunnelled request carrying a public Host header is
    rejected with 421 BEFORE auth ever runs.

    Returning an explicit settings object replaces that implicit allowlist. The
    loopback entries are kept (local clients and health probes must keep
    working) and the operator's public hostnames are added.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*", "127.0.0.1", "localhost"]
    allowed_origins = [
        "http://127.0.0.1:*",
        "http://localhost:*",
        "http://[::1]:*",
    ]

    if not is_loopback(host) and host:
        for form in expand_host_patterns([host]):
            if form not in allowed_hosts:
                allowed_hosts.append(form)

    for form in expand_host_patterns(public_hosts or []):
        if form not in allowed_hosts:
            allowed_hosts.append(form)
        # Connector URLs must be https, so the browser/proxy Origin is https too.
        for scheme in ("https", "http"):
            origin = f"{scheme}://{form}"
            if origin not in allowed_origins:
                allowed_origins.append(origin)

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )


def build_auth(host: str, port: int, token: str):
    """Build ``(AuthSettings, StaticTokenVerifier)`` for the HTTP transport."""
    try:
        from mcp.server.auth.settings import AuthSettings
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise AuthConfigError(f"MCP auth support unavailable: {exc}") from exc

    verifier = StaticTokenVerifier(token)

    # issuer_url and resource_server_url are REQUIRED fields, but with no
    # auth_server_provider and resource_server_url=None neither is ever read:
    # issuer_url is only consumed when mounting OAuth routes (gated on the
    # provider) or the protected-resource document (gated on resource_server_url).
    #
    # resource_server_url stays None deliberately. Publishing
    # /.well-known/oauth-protected-resource would advertise an OAuth
    # authorization server we do not run, inviting clients into a Dynamic Client
    # Registration flow that cannot succeed -- a false claim, not a courtesy.
    #
    # issuer_url is set to the address actually served rather than a placeholder:
    # it is inert by construction today, and if a future release starts reading
    # it, a truthful value degrades gracefully where a fake one would not.
    scheme = "http"
    issuer = f"{scheme}://{host}:{port}"
    auth_settings = AuthSettings(
        issuer_url=issuer,  # inert by construction -- see comment above
        resource_server_url=None,
        required_scopes=[],
    )
    return auth_settings, verifier


# Attribute names this module mutates on the live FastMCP instance. Named here so
# the guard below and the bridge cannot drift apart about what "wired" means.
_VERIFIER_ATTR = "_token_verifier"


def attach(mcp, host: str, port: int, token: str, public_hosts: "list[str] | None" = None):
    """Attach bearer auth + the host allowlist to a live FastMCP instance.

    Why the bridge calls this instead of assigning the fields itself: the
    verifier is attached by setting FastMCP's PRIVATE ``_token_verifier``. The
    constructor cross-validates auth arguments and the bridge's FastMCP is built
    at import time, before the transport is known, so passing them at
    construction is not available. Post-construction assignment works because
    ``streamable_http_app()`` re-reads both fields at call time.

    That private attribute is the risk this function exists to contain. If a
    future mcp release renames it, a bare ``mcp._token_verifier = v`` would
    silently create a NEW attribute that nothing reads -- a server that logs
    "auth ENABLED" and enforces nothing, which is the exact failure mode this
    whole feature exists to prevent. So:

      * refuse if the attribute is not already present on the instance
        (present == the version we validated against; absent == renamed/removed),
      * refuse if ``settings`` has no ``auth`` field,
      * and VERIFY AFTER WRITING that both landed.

    Checks run against the REAL object being mutated, never a throwaway probe --
    a probe can be healthy while the instance in hand is not.
    """
    if not hasattr(mcp, _VERIFIER_ATTR):
        raise AuthConfigError(
            f"incompatible mcp version: FastMCP has no '{_VERIFIER_ATTR}' attribute -- "
            "refusing to start unauthenticated (the token would be silently ignored)"
        )
    settings = getattr(mcp, "settings", None)
    if settings is None or not hasattr(settings, "auth"):
        raise AuthConfigError(
            "incompatible mcp version: FastMCP settings has no 'auth' field -- "
            "refusing to start unauthenticated"
        )

    auth_settings, verifier = build_auth(host, port, token)
    settings.auth = auth_settings
    setattr(mcp, _VERIFIER_ATTR, verifier)
    settings.transport_security = build_transport_security(host, public_hosts)

    # Read back: an assignment that did not stick is indistinguishable from one
    # that was never made, and both serve the catalog unauthenticated.
    if getattr(mcp, _VERIFIER_ATTR, None) is not verifier or settings.auth is not auth_settings:
        raise AuthConfigError(
            "auth wiring did not take effect on the FastMCP instance -- "
            "refusing to start unauthenticated"
        )
    return verifier
