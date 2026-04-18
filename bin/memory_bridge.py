from mcp.server.fastmcp import FastMCP
import inspect
import logging
import sys
import os

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
import memory_core
import memory_sync
import memory_maintenance as _memory_maintenance_mod
import mcp_tool_catalog

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
    if spec.is_async:
        async def _wrapper(args):
            args.pop("__class__", None)  # locals() may include this in some Python versions
            args, err = mcp_tool_catalog.validate_args(spec, args)
            if err:
                return err
            try:
                result = await spec.impl(**args)
                return result if isinstance(result, str) else str(result)
            except Exception as e:
                return f"Error: {type(e).__name__}: {e}"
    else:
        def _wrapper(args):
            args.pop("__class__", None)
            args, err = mcp_tool_catalog.validate_args(spec, args)
            if err:
                return err
            try:
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
def _register_catalog_tools():
    """Register every ToolSpec in mcp_tool_catalog.TOOLS as a FastMCP tool.

    The catalog is the single source of truth for tool name, schema, validators,
    and impl callable. This loop preserves the existing MCP-facing surface
    (44 tools) while making the bridge a thin shim.
    """
    for spec in mcp_tool_catalog.TOOLS:
        _register_one(spec)

def _register_one(spec):
    """Register a single ToolSpec with FastMCP. Builds a typed function per spec
    so FastMCP can introspect the schema and each registered function has the
    right name and docstring."""
    fn = _build_typed_function(spec)
    mcp.tool(name=spec.name, description=spec.description)(fn)

_register_catalog_tools()

# ── Module-level function exposure for test/direct imports ───────────────────
# Each tool is also exposed as a module-level callable so tests and other modules
# can call them directly (e.g. `from memory_bridge import memory_write`).
for _spec in mcp_tool_catalog.TOOLS:
    globals()[_spec.name] = _build_typed_function(_spec)

if __name__ == "__main__":
    logger.info("Memory Bridge (catalog-driven) starting...")
    logger.info(f"Registered {len(mcp_tool_catalog.TOOLS)} MCP tools from mcp_tool_catalog.")
    mcp.run()
