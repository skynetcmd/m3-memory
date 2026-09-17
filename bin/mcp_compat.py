"""Single owner of the MCP server-class import, across mcp 1.x and 2.x.

── WHY THIS MODULE EXISTS ────────────────────────────────────────────────────

mcp 2.0 renamed the server class: `mcp.server.fastmcp.FastMCP` became
`mcp.server.mcpserver.MCPServer`. Eight modules in this repo construct one, and
before this module each carried its own import -- in three different spellings
(a bare `from mcp.server.fastmcp import`, a try/except falling back to
`from mcp import FastMCP`, and one that fell back to a hand-rolled no-op stub).

Copying a try/except into each of those eight sites would be the §10a
duplication defect: eight copies of one policy, free to drift. The resolution
lives here once, and call sites import the NAME.

── WHAT ACTUALLY CHANGED IN 2.x (measured, not assumed) ──────────────────────

Verified against mcp 2.2.0 in a throwaway venv on 2026-09-17. Far less moved
than the rename suggests -- every API this repo uses survived:

    tool()  run()  settings  add_tool()  remove_tool()  list_tools()
    streamable_http_app()  sse_app()  _token_verifier

`run()` and `tool()` keep signatures compatible with our call sites, and
construction still accepts `instructions=`.

ONE real break, and it is not the rename. `Settings` dropped three fields:

    settings.host                  -> ValueError: no field "host"
    settings.port                  -> ValueError: no field "port"
    settings.streamable_http_path  -> ValueError: no field "..."

They moved into the transport call:

    run_streamable_http_async(*, host='127.0.0.1', port=8000,
                              streamable_http_path='/mcp', ...)

`Settings` now carries only auth, debug, dependencies, lifespan, log_level and
the three warn_on_duplicate_* flags. `run_http()` below owns that difference so
no bridge has to branch on it.

── WHAT THIS MODULE DELIBERATELY DOES NOT DO ─────────────────────────────────

It does NOT re-export a stub when mcp is absent. news_fetcher.py previously
fell back to a dummy class whose `tool()` decorator silently did nothing, so a
missing dependency produced a server that started and registered ZERO tools
rather than failing. That is the §3 violation -- a fault that presents as a
working system. A missing mcp now raises at import, where it is legible.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

# The public name. Both spellings resolve to the same server class; 1.x is tried
# first because it is what the pinned floor installs today.
try:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

    MCP_MAJOR = 1
except ImportError:  # mcp 2.x -- renamed, same class
    from mcp.server.mcpserver import MCPServer  # type: ignore[no-redef]

    MCP_MAJOR = 2

if TYPE_CHECKING:  # pragma: no cover
    from mcp.server.transport_security import TransportSecuritySettings

# Alias kept so existing call sites reading `FastMCP` stay valid on both majors.
FastMCP = MCPServer

__all__ = [
    "FastMCP",
    "MCPServer",
    "MCP_MAJOR",
    "run_http",
    "http_app",
    "get_transport_security",
    "set_transport_security",
]

# Where the DNS-rebinding allowlist is parked on 2.x, whose Settings has no
# transport_security field. One spelling, shared by the writer (m3_http_auth),
# the reader below, and run_http.
_TRANSPORT_SECURITY_ATTR = "_m3_transport_security"


def set_transport_security(server: Any, settings: Any) -> None:
    """Attach the DNS-rebinding Host/Origin allowlist to `server`.

    A Settings field on 1.x; on 2.x that assignment raises ValueError, so it is
    stashed on the instance and passed to run() by run_http().
    """
    if MCP_MAJOR == 1:
        server.settings.transport_security = settings
    else:
        setattr(server, _TRANSPORT_SECURITY_ATTR, settings)


def get_transport_security(server: Any) -> Any:
    """Read back whatever set_transport_security() attached, on either major.

    Exists so callers (and tests) never branch on MCP_MAJOR themselves -- the
    2.x location is an implementation detail of this module. Returns None when
    nothing was attached, which for an authenticated server is a fault: it means
    a forged Host header would reach the transport.
    """
    if MCP_MAJOR == 1:
        return getattr(server.settings, "transport_security", None)
    return getattr(server, _TRANSPORT_SECURITY_ATTR, None)


def http_app(server: Any, **kwargs: Any) -> Any:
    """Build the streamable-HTTP ASGI app, with the allowlist actually applied.

    ⚠ Not a cosmetic wrapper. On mcp 1.x `streamable_http_app()` re-reads
    `settings.transport_security` at call time, so attaching it to Settings is
    enough. On 2.x the allowlist is a KEYWORD to this call and the Settings
    field does not exist -- so building the app without passing it yields a
    server with DNS-rebinding protection silently OFF.

    Measured 2026-09-17: routing the tests' `streamable_http_app()` calls
    through this function is what turned a foreign-Host request from ACCEPTED
    back into rejected on 2.x. Build the app here, never directly.
    """
    if MCP_MAJOR == 2:
        ts = kwargs.pop("transport_security", None) or get_transport_security(server)
        if ts is not None:
            kwargs["transport_security"] = ts
    return server.streamable_http_app(**kwargs)


def run_http(
    server: Any,
    *,
    host: str,
    port: int,
    path: str,
    transport_security: "TransportSecuritySettings | None" = None,
) -> None:
    """Run `server` over streamable-http, bound to host/port/path.

    The one place the 1.x/2.x difference is real. On 1.x these are Settings
    fields assigned before `run()`; on 2.x they are keyword arguments TO `run()`
    and assigning them to Settings raises ValueError.

    Gated on MCP_MAJOR rather than on hasattr: a capability probe here would
    silently take the 1.x branch if a future major renamed the fields again,
    and binding to the wrong host is not a failure we want to discover in
    production (§0.5 -- gate on the capability you actually determined).
    """
    if MCP_MAJOR == 1:
        server.settings.host = host
        server.settings.port = port
        server.settings.streamable_http_path = path
        server.run(transport="streamable-http")
        return

    kwargs: dict[str, Any] = {
        "host": host,
        "port": port,
        "streamable_http_path": path,
    }
    # On 2.x the DNS-rebinding allowlist is a run() keyword too. m3_http_auth
    # parks it on the instance (its Settings would reject the assignment); pick
    # it up here so a caller cannot wire auth and then silently drop the
    # allowlist by forgetting an argument.
    if transport_security is None:
        transport_security = get_transport_security(server)
    if transport_security is not None:
        kwargs["transport_security"] = transport_security
    server.run(transport="streamable-http", **kwargs)
