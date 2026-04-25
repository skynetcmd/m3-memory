---
name: m3:find-in-chat
description: Search captured chat-log turns from your prior Claude / Gemini sessions.
argument-hint: <query>
---

Call `m3-memory:chatlog_search` with `query="$ARGUMENTS"`, `k=10`.

Group results by `conversation_id` and present chronologically. For each match show:
- timestamp + host_agent (claude-code / gemini-cli)
- model_id
- 2-line excerpt with the matching span highlighted

If results span more than one conversation, mention that — the user may want to drill into a specific session via `chatlog_list_conversations` (a related MCP tool).
