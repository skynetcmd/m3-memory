import json
import asyncio
import os
import sys

# Ensure bin is in the path to import from memory_bridge
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
bin_dir = os.path.join(BASE_DIR, "bin")
if bin_dir not in sys.path:
    sys.path.insert(0, bin_dir)

from memory_bridge import memory_write, memory_search, memory_delete, memory_update
from memory_core import _db

def add_knowledge(content: str, title: str = "", source: str = "", tags: list[str] = None, item_type: str = "knowledge", metadata: str = "") -> str:
    """Adds a new knowledge item to memory."""
    tags = tags or []
    # If custom metadata JSON is provided, use it; otherwise build from source/tags
    if metadata:
        final_metadata = metadata
    else:
        final_metadata = json.dumps({"source": source, "tags": tags})
    
    try:
        # Check if an event loop is already running
        asyncio.get_running_loop()
        return "Error: add_knowledge called from running event loop. Use async version."
    except RuntimeError:
        return asyncio.run(memory_write(
            type=item_type,
            content=content,
            title=title,
            metadata=final_metadata
        ))

def update_knowledge(item_id: str, content: str = "", title: str = "", metadata: str = "", importance: float = -1.0, reembed: bool = False) -> str:
    """Updates an existing knowledge item."""
    try:
        asyncio.get_running_loop()
        return "Error: update_knowledge called from running event loop. Use async version."
    except RuntimeError:
        return asyncio.run(memory_update(
            id=item_id,
            content=content,
            title=title,
            metadata=metadata,
            importance=importance,
            reembed=reembed
        ))

def search_knowledge(query: str, k: int = 8, type_filter: str = "") -> str:
    """Searches knowledge items using hybrid semantic/keyword search."""
    try:
        asyncio.get_running_loop()
        return "Error: search_knowledge called from running event loop."
    except RuntimeError:
        return asyncio.run(memory_search(
            query=query,
            k=k,
            type_filter=type_filter
        ))

def get_all_types() -> list[str]:
    """Returns a list of all distinct memory item types in the database."""
    with _db() as db:
        rows = db.execute("SELECT DISTINCT type FROM memory_items WHERE type IS NOT NULL AND type != '' ORDER BY type").fetchall()
        return [r["type"] for r in rows]

def list_knowledge(limit: int = 50, type_filter: str = "") -> list[dict]:
    """Lists recent knowledge items without semantic search."""
    with _db() as db:
        if type_filter:
            is_exact = (type_filter.startswith('"') and type_filter.endswith('"')) or (type_filter.startswith("'") and type_filter.endswith("'"))
            actual_type = type_filter[1:-1] if is_exact else type_filter
            if is_exact:
                sql = """
                    SELECT id, type, title, content, metadata_json, created_at, importance
                    FROM memory_items
                    WHERE type = ? AND is_deleted = 0
                    ORDER BY created_at DESC
                    LIMIT ?
                """
            else:
                sql = """
                    SELECT id, type, title, content, metadata_json, created_at, importance
                    FROM memory_items
                    WHERE type LIKE ? AND is_deleted = 0
                    ORDER BY created_at DESC
                    LIMIT ?
                """
            params = (actual_type, limit)
        else:
            sql = """
                SELECT id, type, title, content, metadata_json, created_at, importance
                FROM memory_items
                WHERE type NOT IN ('conversation', 'message', 'thought') AND is_deleted = 0
                ORDER BY created_at DESC
                LIMIT ?
            """
            params = (limit,)
            
        rows = db.execute(sql, params).fetchall()
        
        results = []
        for row in rows:
            r = dict(row)
            meta = {}
            if r["metadata_json"]:
                try:
                    meta = json.loads(r["metadata_json"])
                except json.JSONDecodeError:
                    pass
            
            results.append({
                "id": r["id"],
                "type": r["type"],
                "title": r["title"],
                "content": r["content"],
                "source": meta.get("source", ""),
                "tags": meta.get("tags", []),
                "created_at": r["created_at"],
                "importance": r["importance"]
            })
        return results

def delete_knowledge(item_id: str, hard: bool = False) -> str:
    """Deletes a knowledge item by ID."""
    # memory_delete is synchronous in memory_bridge.py
    return memory_delete(item_id, hard=hard)
