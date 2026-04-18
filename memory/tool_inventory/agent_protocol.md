---
tool: bin/agent_protocol.py
sha1: 534e0e4e2dfa
mtime_utc: 2026-04-18T03:45:31.258358+00:00
generated_utc: 2026-04-18T05:16:53.060503+00:00
private: false
---

# bin/agent_protocol.py

## Purpose

Translation layer for OpenAI-shape payloads across M3 Max MCP bridges (Anthropic, Gemini, Grok, LM Studio). Handles bidirectional message/tool schema conversion and multi-provider response parsing.

## Entry points / Public API

- `openai_to_anthropic(messages, model)` (line 12) — Convert OpenAI messages to Anthropic format
- `openai_to_gemini(messages, model)` (line 37) — Convert OpenAI messages to Gemini format
- `openai_to_anthropic_with_tools(messages, model, tools=None, max_tokens=4096)` (line 179) — Tool-aware Anthropic conversion
- `openai_to_gemini_with_tools(messages, model, tools=None)` (line 243) — Tool-aware Gemini conversion
- `openai_tools_to_anthropic_tools(tools)` (line 136) — Convert OpenAI tool schema to Anthropic
- `openai_tools_to_gemini_tools(tools)` (line 151) — Convert OpenAI tool schema to Gemini
- `parse_tool_calls(raw_response, provider)` (line 299) — Extract text + tool_calls from any provider response
- `translate_response(raw_resp, source)` (line 62) — Normalize provider responses to OpenAI format
- `extract_reasoning(content)` (line 89) — Parse DeepSeek `<think>` tags
- `format_tool_result(tool_call_id, name, result, is_error=False)` (line 351) — Build tool message for dispatch loop

## CLI flags / arguments

_(no CLI surface — library module only)_

## Environment variables read

_(none)_

## Calls INTO this repo (intra-repo imports)

_(none)_

## Calls OUT (external side-channels)

_(none — pure translation logic, no I/O)_

## File dependencies

_(none)_

## Re-validation

If `sha1` differs from current file's sha1, re-read source and regenerate via `python bin/gen_tool_inventory.py`.
