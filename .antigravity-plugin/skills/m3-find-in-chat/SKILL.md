---
name: m3-find-in-chat
description: Search captured chat-log turns from your prior Claude / Gemini / Antigravity sessions.
---
# M3 Find In Chat

## When to Use
Use this skill when the user wants to search past conversation transcripts or look up specific things they discussed with the assistant in prior turns or sessions.

## Instructions
Call the `m3:chatlog_search` MCP tool with `query="$ARGUMENTS"`, `k=10`.

Group results by `conversation_id` and present chronologically. For each match show:
- timestamp + host_agent (claude-code / gemini-cli / antigravity-cli)
- model_id
- 2-line excerpt with the matching span highlighted

If results span more than one conversation, mention that — the user may want to drill into a specific session via `chatlog_list_conversations` (a related MCP tool).
