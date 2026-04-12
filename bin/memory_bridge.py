from mcp.server.fastmcp import FastMCP
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

# ── Validation Constants ─────────────────────────────────────────────────────
MAX_CONTENT_SIZE = 50_000       # characters
MAX_QUERY_LENGTH = 2_000        # characters
MAX_K = 100
VALID_MEMORY_TYPES = frozenset({
    "note", "fact", "decision", "preference", "conversation", "message",
    "task", "code", "config", "observation", "plan", "summary", "snippet",
    "reference", "log", "home", "user_fact", "scratchpad", "auto",
})

# Re-export internal helpers for legacy compatibility (C7, H11)
_conn = memory_core._conn
_embed = memory_core._embed
_db = memory_core._db
_ensure_sync_tables = memory_core._ensure_sync_tables
_content_hash       = memory_core._content_hash
_pack               = memory_core._pack

# ── MCP Tools (Proxied to implementations) ────────────────────────────────────

@mcp.tool()
async def memory_write(type, content, title="", metadata="{}", agent_id="", model_id="", change_agent="", importance=0.5, source="agent", embed=True, user_id="", scope="agent", valid_from="", valid_to="", auto_classify=False):
    """Creates a MemoryItem and optionally embeds it for semantic search. Contradiction detection is automatic — if new content conflicts with an existing memory of the same type/title, the old one is superseded. Use type='auto' to let the LLM decide the best category."""
    import json as _json
    # Input validation
    if type not in VALID_MEMORY_TYPES:
        return f"Error: invalid memory type '{type}'. Valid types: {', '.join(sorted(VALID_MEMORY_TYPES))}"
    if content and len(content) > MAX_CONTENT_SIZE:
        return f"Error: content too large ({len(content)} chars). Maximum is {MAX_CONTENT_SIZE}."
    if isinstance(metadata, dict):
        metadata = _json.dumps(metadata)
    elif isinstance(metadata, str) and metadata and metadata != "{}":
        try:
            _json.loads(metadata)
        except (ValueError, _json.JSONDecodeError):
            return "Error: metadata is not valid JSON."
    
    # If type is "auto", force auto_classify to True
    if type == "auto":
        auto_classify = True

    return await memory_core.memory_write_impl(type, content, title, metadata, agent_id, model_id, change_agent, importance, source, embed, user_id, scope, valid_from, valid_to, auto_classify)

@mcp.tool()
async def memory_search(query, k=8, type_filter="", agent_filter="", search_mode="hybrid", include_scratchpad=False, user_id="", scope="", as_of=""):
    """Search across memory items using semantic similarity or keyword matching. Filter by user_id and scope for isolation."""
    if not query or not str(query).strip():
        return "Error: query cannot be empty."
    query = str(query)
    if len(query) > MAX_QUERY_LENGTH:
        query = query[:MAX_QUERY_LENGTH]
    try:
        k = int(k)
    except (TypeError, ValueError):
        k = 8
    k = max(1, min(k, MAX_K))
    return await memory_core.memory_search_impl(query, k, type_filter, agent_filter, search_mode, include_scratchpad, user_id, scope, as_of)

@mcp.tool()
async def memory_suggest(query, k=5):
    """Preview which memories would be retrieved for a query, with score breakdowns explaining why each was selected."""
    return await memory_core.memory_suggest_impl(query, int(k))

@mcp.tool()
def memory_get(id):
    """Retrieves a full MemoryItem by UUID."""
    return memory_core.memory_get_impl(id)

@mcp.tool()
async def memory_update(id, content="", title="", metadata="", importance=-1.0, reembed=False):
    """Updates a MemoryItem by ID."""
    import json as _json
    if isinstance(metadata, dict):
        metadata = _json.dumps(metadata)
    return await memory_core.memory_update_impl(id, content, title, metadata, importance, reembed)

@mcp.tool()
def memory_delete(id, hard=False):
    """Deletes a MemoryItem (soft or hard)."""
    return memory_core.memory_delete_impl(id, hard)

@mcp.tool()
async def conversation_start(title, agent_id="", model_id="", tags=""):
    """Starts a new conversation thread."""
    return await memory_core.conversation_start_impl(title, agent_id, model_id, tags)

@mcp.tool()
async def conversation_append(conversation_id, role, content, agent_id="", model_id="", embed=True):
    """Appends a message to a conversation."""
    return await memory_core.conversation_append_impl(conversation_id, role, content, agent_id, model_id, embed)

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

@mcp.tool()
async def conversation_search(query, k=8):
    """Search messages across conversations using hybrid semantic/keyword search."""
    return await memory_core.memory_search_impl(query, k=k, type_filter="message")

@mcp.tool()
async def conversation_summarize(conversation_id, threshold=20):
    """Summarize a conversation into key points using the local LLM."""
    return await memory_core.conversation_summarize_impl(conversation_id, int(threshold))

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

@mcp.tool()
async def chroma_sync(max_items=50, direction="both", reset_stalled=True):
    """Bi-directional sync between local SQLite and ChromaDB."""
    return await memory_sync.chroma_sync_impl(max_items, direction, reset_stalled)

@mcp.tool()
def memory_maintenance(decay=True, purge_expired=True, prune_orphan_embeddings=True):
    """Runs maintenance tasks on the memory store."""
    return _memory_maintenance_mod.memory_maintenance_impl(decay, purge_expired, prune_orphan_embeddings)

@mcp.tool()
async def memory_consolidate(type_filter="", agent_filter="", threshold=20):
    """Consolidate old memories of the same type into summaries using the local LLM. Reduces clutter while preserving knowledge."""
    return await _memory_maintenance_mod.memory_consolidate_impl(type_filter, agent_filter, int(threshold))

@mcp.tool()
def memory_dedup(threshold=0.92, dry_run=True):
    """Find and merge near-duplicate memory items."""
    return _memory_maintenance_mod.memory_dedup_impl(threshold, dry_run)

@mcp.tool()
def memory_feedback(memory_id, feedback="useful"):
    """Provide feedback on a memory item to improve quality."""
    return _memory_maintenance_mod.memory_feedback_impl(memory_id, feedback)

@mcp.tool()
def memory_history(memory_id, limit=20):
    """Returns the change history (audit trail) for a memory item. Tracks create, update, delete, and supersede events."""
    return memory_core.memory_history_impl(memory_id, limit)

@mcp.tool()
def memory_link(from_id, to_id, relationship_type="related"):
    """Creates a directional link between two memory items. Valid types: related, supports, contradicts, extends, supersedes, references, consolidates, message, handoff."""
    return memory_core.memory_link_impl(from_id, to_id, relationship_type)

@mcp.tool()
def memory_graph(memory_id, depth=1):
    """Returns the local graph neighborhood of a memory item (connected memories up to N hops, max 3)."""
    return memory_core.memory_graph_impl(memory_id, depth)

@mcp.tool()
def memory_verify(id):
    """Verify content integrity by comparing stored hash with computed hash. Returns OK if content hasn't been tampered with."""
    return memory_core.memory_verify_impl(id)

@mcp.tool()
def memory_set_retention(agent_id, max_memories=1000, ttl_days=0, auto_archive=1):
    """Set or update per-agent memory retention policy. Controls max memory count, TTL expiry, and auto-archival."""
    try:
        max_memories = int(max_memories)
        ttl_days = int(ttl_days)
        auto_archive = int(auto_archive)
    except (TypeError, ValueError):
        return "Error: max_memories, ttl_days, and auto_archive must be integers."
    return _memory_maintenance_mod.memory_set_retention_impl(agent_id, max_memories, ttl_days, auto_archive)

@mcp.tool()
def memory_export(agent_filter="", type_filter="", since=""):
    """Export memories as portable JSON. Filter by agent, type, or date."""
    return _memory_maintenance_mod.memory_export_impl(agent_filter, type_filter, since)

@mcp.tool()
def memory_import(data):
    """Import memories from a JSON export. UPSERT semantics — safe to re-run."""
    return _memory_maintenance_mod.memory_import_impl(data)

@mcp.tool()
def gdpr_export(user_id):
    """Export all memories for a data subject (GDPR data portability). Returns JSON with all memory items for the given user_id."""
    if not user_id or not str(user_id).strip():
        return "Error: user_id is required."
    return _memory_maintenance_mod.gdpr_export_impl(str(user_id).strip())

@mcp.tool()
def gdpr_forget(user_id):
    """Right to be forgotten — hard-deletes ALL data for a user_id including memories, embeddings, relationships, and history."""
    if not user_id or not str(user_id).strip():
        return "Error: user_id is required."
    return _memory_maintenance_mod.gdpr_forget_impl(str(user_id).strip())

@mcp.tool()
def memory_cost_report():
    """Returns current session operation counts and estimated token usage for memory operations."""
    return memory_core.memory_cost_report_impl()

@mcp.tool()
def memory_handoff(from_agent: str, to_agent: str, task: str,
                   context_ids: list = None, note: str = "") -> str:
    """Hand off a task from one agent to another. Writes a new handoff-type
    memory owned to_agent and links it to the given context memories with
    'handoff' edges. Returns a confirmation string with the new memory id.

    Note: this is the in-process memory handoff (memory_items + memory_relationships).
    Unrelated to the standalone session_handoff.py MCP server."""
    return memory_core.memory_handoff_impl(from_agent, to_agent, task,
                                           context_ids or [], note)

@mcp.tool()
def memory_inbox(agent_id: str, unread_only: bool = True, limit: int = 20) -> str:
    """List handoff messages addressed to agent_id, newest first.
    Pass unread_only=False to include already-acked items."""
    return memory_core.memory_inbox_impl(agent_id, bool(unread_only), int(limit))

@mcp.tool()
def memory_inbox_ack(memory_id: str) -> str:
    """Mark a handoff memory as read (sets read_at = now)."""
    return memory_core.memory_inbox_ack_impl(memory_id)

if __name__ == "__main__":
    logger.info("Memory Bridge (Modular) starting...")
    mcp.run()
