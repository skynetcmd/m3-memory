---
name: m3-tasks
description: List your tasks and their state. Pass an argument to filter by state.
---
# M3 Tasks

## When to Use
Use this skill when the user wants to list tasks they or the agents are tracking, or check status of tasks in the workspace task graph.

## Instructions
Call `m3:task_list`. If `$ARGUMENTS` is one of `pending`, `in_progress`, `completed`, `deleted`, pass it as the `state` filter.

Render as a table:
```
| id-prefix | state         | subject                       | owner          |
```

If filtering produced no results, say so explicitly so the user doesn't think the call broke.
