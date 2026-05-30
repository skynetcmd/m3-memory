---
name: m3-graph
description: Show memories related to a given one — knowledge-graph traversal up to 3 hops.
---
# M3 Graph

## When to Use
Use this skill when you want to visualize the connections, dependencies, contradictions, and references of a specific memory in the local Knowledge Graph.

## Instructions
Call `m3:memory_graph` with `id="$ARGUMENTS"`, `depth=2`.

Render as an indented tree: the root memory at the top, each related memory underneath with the relationship type and direction (`→ supersedes`, `← derived_from`, etc.). Truncate content to 80 chars per node.

If the result is empty, the memory has no recorded relationships yet. Suggest `/m3:search` for content-similar items instead.
