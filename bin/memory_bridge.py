import inspect
import logging
import os
import sys

from m3_sdk import active_database
from mcp.server.fastmcp import FastMCP

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(name)s: [%(levelname)s] %(message)s',
    stream=sys.stderr,
)
logger = logging.getLogger("memory_bridge")

mcp = FastMCP("Memory Bridge")

# Modular imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_tool_catalog
import memory_core
import tool_domains
import tool_loader

# Re-export internal helpers for legacy compatibility (C7, H11)
_conn = memory_core._conn
_embed = memory_core._embed
_db = memory_core._db
_ensure_sync_tables = memory_core._ensure_sync_tables
_content_hash       = memory_core._content_hash
_pack               = memory_core._pack

# Re-export catalog validation constants — tests and external callers import
# VALID_MEMORY_TYPES from memory_bridge directly.
VALID_MEMORY_TYPES = mcp_tool_catalog.VALID_MEMORY_TYPES
VALID_ENTITY_TYPES = mcp_tool_catalog.VALID_ENTITY_TYPES
VALID_ENTITY_PREDICATES = mcp_tool_catalog.VALID_ENTITY_PREDICATES
MAX_CONTENT_SIZE   = mcp_tool_catalog.MAX_CONTENT_SIZE
MAX_QUERY_LENGTH   = mcp_tool_catalog.MAX_QUERY_LENGTH
MAX_K              = mcp_tool_catalog.MAX_K

# ── Helper functions still used directly by tests / other modules ────────────
def conversation_messages(conversation_id):
    """Returns all messages in a conversation as a formatted string (role: content)."""
    with memory_core._db() as db:
        rows = db.execute(
            """SELECT mi.title AS role, mi.content, mi.created_at
               FROM memory_relationships mr
               JOIN memory_items mi ON mr.to_id = mi.id
               WHERE mr.from_id = ? AND mr.relationship_type = 'message' AND mi.is_deleted = 0
               ORDER BY mi.created_at ASC""",
            (conversation_id,)
        ).fetchall()
    if not rows:
        return f"Error: no messages found for conversation {conversation_id}"
    return "\n".join(f"{row['role']}: {row['content']}" for row in rows)

def sync_status():
    """Returns a summary string of the Chroma sync queue, mirror, and conflict counts."""
    try:
        with memory_core._db() as db:
            row = db.execute("SELECT COUNT(*) FROM chroma_sync_queue").fetchone()
            queue_count = row[0] if row else 0
            row = db.execute("SELECT COUNT(*) FROM chroma_mirror").fetchone()
            mirror_count = row[0] if row else 0
            row = db.execute("SELECT COUNT(*) FROM sync_conflicts WHERE resolution = 'pending'").fetchone()
            conflict_count = row[0] if row else 0
        return f"Queue: {queue_count} | Mirror: {mirror_count} | Conflicts: {conflict_count}"
    except Exception as e:
        return f"Sync status unavailable: {e}"

# ── Typed function builder for FastMCP schema introspection ──────────────────
def _build_typed_function(spec):
    """Build a typed function from spec.parameters so FastMCP can introspect it.

    Returns a function with explicit typed parameters that FastMCP can use to
    generate proper JSONSchema. Both async and sync are supported.
    """
    props = spec.parameters.get("properties", {})
    required = set(spec.parameters.get("required", []))

    # Preserve the impl's natural parameter order so positional calls keep working.
    # Tests and other callers do `task_create("title", created_by=...)` — alphabetical
    # sorting would put `created_by` at position 0 and break that contract. Use
    # inspect.signature on the impl to recover the canonical order, then drop any
    # parameter that isn't in the catalog's properties.
    try:
        impl_param_order = [
            p.name for p in inspect.signature(spec.impl).parameters.values()
            if p.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        ]
    except (TypeError, ValueError):
        impl_param_order = list(props.keys())

    ordered = [n for n in impl_param_order if n in props]
    # Append any catalog property the impl signature didn't surface (rare — mostly
    # the inline _conversation_search_impl path). Order is stable.
    for n in props.keys():
        if n not in ordered:
            ordered.append(n)

    parts = []
    for pname in ordered:
        pdef = props[pname]
        ptype = {
            "string": "str", "integer": "int", "number": "float",
            "boolean": "bool", "array": "list", "object": "dict",
        }.get(pdef.get("type", "string"), "str")

        if pname in required:
            parts.append(f"{pname}: {ptype}")
        else:
            default = pdef.get("default", None)
            if isinstance(default, bool):           # before int — bool is an int subclass
                default_repr = str(default)
            elif isinstance(default, str):
                default_repr = repr(default)        # repr handles quoting + escapes
            elif isinstance(default, (int, float)):
                default_repr = str(default)
            elif default is None:
                default_repr = "None"
            else:
                default_repr = repr(default)
            parts.append(f"{pname}: {ptype} = {default_repr}")

    sig = ", ".join(parts)

    # Build the function source. The _wrapper closure will call the validator and impl.
    if spec.is_async:
        src = f"async def _impl({sig}):\n    return await _wrapper(locals())"
    else:
        src = f"def _impl({sig}):\n    return _wrapper(locals())"

    # The _wrapper closes over spec; locals() gives us the bound args.
    # `database` is a universal injected parameter (see mcp_tool_catalog
    # ._inject_database_arg); pop it before validators/impl run and activate
    # the corresponding M3Context for the duration of the call.
    if spec.is_async:
        async def _wrapper(args):
            args.pop("__class__", None)  # locals() may include this in some Python versions
            database = mcp_tool_catalog._pop_database(args)
            args, err = mcp_tool_catalog.validate_args(spec, args)
            if err:
                return err
            try:
                with active_database(database):
                    result = await spec.impl(**args)
                return result if isinstance(result, str) else str(result)
            except Exception as e:
                return f"Error: {type(e).__name__}: {e}"
    else:
        def _wrapper(args):
            args.pop("__class__", None)
            database = mcp_tool_catalog._pop_database(args)
            args, err = mcp_tool_catalog.validate_args(spec, args)
            if err:
                return err
            try:
                with active_database(database):
                    result = spec.impl(**args)
                return result if isinstance(result, str) else str(result)
            except Exception as e:
                return f"Error: {type(e).__name__}: {e}"

    ns = {"_wrapper": _wrapper}
    exec(src, ns)  # nosec B102 - src is built from static ToolSpec catalog, not user input
    fn = ns["_impl"]
    fn.__name__ = spec.name
    fn.__doc__ = spec.description
    return fn

# ── Catalog-driven MCP tool registration ──────────────────────────────────────
# Lazy mode (default): only the essentials + the two meta-tools register at
# startup. Other tools register on demand when the agent calls
# `tools_load_domain(domain=…)`. Set M3_TOOLS_LAZY=0 to disable and expose
# every tool at startup (legacy behavior).
_LAZY_MODE = os.environ.get("M3_TOOLS_LAZY", "1") != "0"

# Tracks which tools are currently registered with FastMCP. Starts with the
# essentials + the two meta-tools, grows as domains are loaded.
_REGISTERED: set[str] = set()


def _register_one(spec):
    """Register a single ToolSpec with FastMCP. Builds a typed function per spec
    so FastMCP can introspect the schema and each registered function has the
    right name and docstring. Idempotent — registering the same spec twice is
    a no-op."""
    if spec.name in _REGISTERED:
        return False
    fn = _build_typed_function(spec)
    mcp.tool(name=spec.name, description=spec.description)(fn)
    _REGISTERED.add(spec.name)
    return True


def _register_initial_tools():
    """Initial registration set, called once at startup.

    Lazy mode: meta-tools + essentials only (~9 tools — 2 meta + 7
    essentials per tool_domains.ESSENTIAL_TOOL_NAMES — ~3.2 K tokens).
    Eager mode: every ToolSpec (~85 tools, ~15.8 K tokens — pre-2026-05 behavior).
    """
    _META_TOOLS = {"tools_list_domains", "tools_load_domain"}
    for spec in mcp_tool_catalog.TOOLS:
        if not _LAZY_MODE:
            _register_one(spec)
            continue
        if spec.name in _META_TOOLS or tool_domains.is_essential(spec.name):
            _register_one(spec)


def _register_domain_callback(domain: str) -> dict:
    """Register every ToolSpec belonging to the given domain.

    Called by `tool_loader.load_domain()` when the agent invokes the
    `tools_load_domain` MCP tool. Returns a summary dict the meta-tool
    serializes back to the agent.

    The MCP `notifications/tools/list_changed` notification is emitted by
    FastMCP automatically whenever `mcp.tool(...)` registers a new tool
    after the session is live — clients that advertise the `tools.listChanged`
    capability will re-fetch the catalog. For clients that don't, the
    returned dict carries the full schema list as a fallback the agent can
    use in-band.
    """
    newly_registered: list[str] = []
    schemas: list[dict] = []
    for spec in mcp_tool_catalog.TOOLS:
        if tool_domains.domain_of_tool(spec.name) != domain:
            continue
        if _register_one(spec):
            newly_registered.append(spec.name)
            schemas.append({
                "name": spec.name,
                "description": spec.description,
                "inputSchema": spec.parameters,
            })

    return {
        "domain": domain,
        "tools_now_available": newly_registered,
        "tools_total_registered": len(_REGISTERED),
        # Always return schemas — clients with listChanged support will ignore
        # them; clients without it use them in-band. Small payload either way.
        "schemas": schemas,
    }


# Wire the meta-tool callback BEFORE we register tools, so the meta-tools
# work the moment they go live.
tool_loader.set_register_domain_callback(_register_domain_callback)

_register_initial_tools()


# ── Module-level function exposure for test/direct imports ───────────────────
# Each tool is also exposed as a module-level callable so tests and other modules
# can call them directly (e.g. `from memory_bridge import memory_write`).
# We expose EVERY tool here regardless of lazy mode — tests bypass MCP and
# need direct access to all impls.
for _spec in mcp_tool_catalog.TOOLS:
    globals()[_spec.name] = _build_typed_function(_spec)

if __name__ == "__main__":
    import os as _os
    logger.info("Memory Bridge (catalog-driven) starting...")
    # B17: detect version drift between currently-imported package and the
    # previous boot. Warns (without aborting) if the user upgraded
    # m3-memory while the old server is still running.
    try:
        from version_drift import check_and_record as _check_drift
        _drift = _check_drift()
        logger.info(
            f"version: {_drift.get('current_version')} "
            f"(prior boot: {_drift.get('prior_version') or 'first run'})"
        )
    except Exception as _e:
        logger.debug(f"version-drift check skipped: {type(_e).__name__}: {_e}")
    if _LAZY_MODE:
        logger.info(
            f"Lazy mode: registered {len(_REGISTERED)} essentials+meta tools at startup "
            f"(out of {len(mcp_tool_catalog.TOOLS)} total). "
            f"Use `tools_load_domain` to expose more. Set M3_TOOLS_LAZY=0 to disable."
        )
    else:
        logger.info(f"Eager mode: registered all {len(_REGISTERED)} MCP tools at startup.")

    # Transport selection.
    #   stdio (default): MCP client launches us as a subprocess and pipes JSON-RPC
    #     over stdin/stdout. This is how Claude Code, Gemini CLI, Aider, etc. talk
    #     to us out of the box.
    #   streamable-http: FastMCP's HTTP+SSE transport. Lets remote clients like
    #     claude.ai connect via the MCP Connector — they need a URL, not a local
    #     process. Self-host on a box, expose via cloudflared / tailscale / ngrok,
    #     paste the URL into claude.ai's connector settings.
    #
    # Env vars (mirror the `mcp-memory serve` CLI flags):
    #   M3_TRANSPORT=stdio | http        (default: stdio)
    #   M3_HTTP_HOST=127.0.0.1           (default: localhost-only — bind to 0.0.0.0
    #                                     ONLY behind a reverse proxy / tunnel)
    #   M3_HTTP_PORT=8080                (default: 8080)
    #   M3_HTTP_PATH=/mcp                (default mount path)
    transport = _os.environ.get("M3_TRANSPORT", "stdio").lower().strip()
    if transport in ("http", "streamable-http", "streamable_http"):
        host = _os.environ.get("M3_HTTP_HOST", "127.0.0.1")
        port = int(_os.environ.get("M3_HTTP_PORT", "8080"))
        path = _os.environ.get("M3_HTTP_PATH", "/mcp")
        logger.info(f"Transport: streamable-http on http://{host}:{port}{path}")
        # FastMCP exposes the host/port/path settings via its Settings object.
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.settings.streamable_http_path = path
        mcp.run(transport="streamable-http")
    else:
        logger.info("Transport: stdio")
        mcp.run()
