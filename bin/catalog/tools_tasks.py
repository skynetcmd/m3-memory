"""catalog.tools_tasks — tasks-domain ToolSpec entries (8).

Split out of mcp_tool_catalog.py's flat TOOLS list. Entries copied verbatim
(name, description, parameters, impl, validators, flags unchanged). Domain
assignment per tool_domains.domain_of_tool().
"""
from __future__ import annotations

from .lazy import LazyModuleProxy
from .spec import ToolSpec

memory_core = LazyModuleProxy("memory_core")


TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="task_create",
        description="Create a new task in 'pending' state. Returns task id.",
        parameters={
            "type": "object",
            "properties": {
                "title":          {"type": "string", "description": "Task title."},
                "created_by":     {"type": "string", "description": "Agent or user that created the task."},
                "description":    {"type": "string", "description": "Longer description.", "default": ""},
                "owner_agent":    {"type": "string", "description": "Initial owner (blank = unassigned).", "default": ""},
                "parent_task_id": {"type": "string", "description": "Optional parent task id for sub-tasks.", "default": ""},
                "metadata":       {"type": "object", "description": "Free-form metadata.", "default": {}},
            },
            "required": ["title", "created_by"],
        },
        impl=memory_core.task_create_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_assign",
        description="Assign a task to an owner. Sets state=in_progress and notifies the new owner.",
        parameters={
            "type": "object",
            "properties": {
                "task_id":     {"type": "string", "description": "Task UUID."},
                "owner_agent": {"type": "string", "description": "Agent to assign to."},
            },
            "required": ["task_id", "owner_agent"],
        },
        impl=memory_core.task_assign_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_update",
        description="Partial update for a task. Validates state transitions. On terminal state, sets completed_at.",
        parameters={
            "type": "object",
            "properties": {
                "task_id":     {"type": "string", "description": "Task UUID."},
                "state":       {"type": "string", "enum": ["", "pending", "in_progress", "blocked", "completed", "failed", "cancelled"], "description": "New state (empty = no change).", "default": ""},
                "description": {"type": "string", "description": "New description (empty = no change).", "default": ""},
                "metadata":    {"type": "object", "description": "New metadata (empty = no change).", "default": {}},
                "actor":       {"type": "string", "description": "Actor making the update.", "default": ""},
            },
            "required": ["task_id"],
        },
        impl=memory_core.task_update_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_delete",
        description="Delete a task. Soft-delete (default) sets a tombstone that propagates via pg_sync to the warehouse and peers. Hard-delete removes the row locally and requires a prior soft-delete.",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task UUID."},
                "hard":    {"type": "boolean", "description": "If true, permanently remove an already-tombstoned row from local SQLite.", "default": False},
                "actor":   {"type": "string", "description": "Actor performing the delete (audit log).", "default": ""},
            },
            "required": ["task_id"],
        },
        impl=memory_core.task_delete_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_set_result",
        description="Set the result memory pointer for a task. Does NOT change state.",
        parameters={
            "type": "object",
            "properties": {
                "task_id":         {"type": "string", "description": "Task UUID."},
                "result_memory_id": {"type": "string", "description": "Result memory UUID."},
            },
            "required": ["task_id", "result_memory_id"],
        },
        impl=memory_core.task_set_result_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_get",
        description="Get full record for one task.",
        parameters={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task UUID."},
            },
            "required": ["task_id"],
        },
        impl=memory_core.task_get_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_list",
        description="List tasks with optional filters. Newest updated first.",
        parameters={
            "type": "object",
            "properties": {
                "owner_agent":   {"type": "string", "description": "Filter by owner agent.", "default": ""},
                "state":         {"type": "string", "description": "Filter by task state.", "default": ""},
                "parent_task_id": {"type": "string", "description": "Filter by parent task id.", "default": ""},
                "limit":         {"type": "integer", "description": "Max tasks to return.", "default": 50},
            },
            "required": [],
        },
        impl=memory_core.task_list_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
    ToolSpec(
        name="task_tree",
        description="Render a recursive subtree of tasks rooted at root_task_id.",
        parameters={
            "type": "object",
            "properties": {
                "root_task_id": {"type": "string", "description": "Root task UUID."},
                "max_depth":    {"type": "integer", "description": "Max recursion depth.", "default": 3},
            },
            "required": ["root_task_id"],
        },
        impl=memory_core.task_tree_impl,
        is_async=False,
        validators=(),
        default_allowed=True,
        inject_agent_id=False,
    ),
]
