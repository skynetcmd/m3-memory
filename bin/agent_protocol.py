import json
import re
import uuid

# DeepSeek-R1 and similar reasoning models emit <think>...</think> chains.
# The capturing group in _THINK_TAG_RE returns the inner text; re.sub with
# the same pattern drops it in-place. Module-scope so every bridge that
# imports agent_protocol shares one compiled pattern.
_THINK_TAG_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


class AgentProtocol:
    """
    Unified translation layer for OpenAI-style payloads across M3 Max MCP bridges.
    Handles mapping to/from Anthropic, Gemini, Grok, and LM Studio.
    """

    @staticmethod
    def openai_to_anthropic(messages: list, model: str) -> dict:
        """Translates OpenAI messages to Anthropic's format."""
        system = ""
        anthropic_messages = []

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            if role == "system":
                system = content
            else:
                anthropic_messages.append({
                    "role": role if role != "assistant" else "assistant",
                    "content": content
                })

        return {
            "model": model,
            "system": system,
            "messages": anthropic_messages,
            "max_tokens": 4096
        }

    @staticmethod
    def openai_to_gemini(messages: list, model: str) -> dict:
        """Translates OpenAI messages to Google Gemini's format."""
        contents = []
        system_instruction = None

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")

            if role == "system":
                system_instruction = {"parts": [{"text": content}]}
            else:
                gemini_role = "user" if role == "user" else "model"
                contents.append({
                    "role": gemini_role,
                    "parts": [{"text": content}]
                })

        payload = {"contents": contents}
        if system_instruction:
            payload["system_instruction"] = system_instruction

        return payload

    @staticmethod
    def translate_response(raw_resp: dict, source: str) -> dict:
        """Translates backend-specific responses to OpenAI format."""
        if source == "anthropic":
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": raw_resp.get("content", [{"text": ""}])[0].get("text", "")
                    },
                    "finish_reason": raw_resp.get("stop_reason", "stop")
                }]
            }
        elif source == "gemini":
            candidates = raw_resp.get("candidates", [{}])
            content = candidates[0].get("content", {}).get("parts", [{"text": ""}])[0].get("text", "")
            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": content
                    },
                    "finish_reason": candidates[0].get("finish_reason", "stop")
                }]
            }
        return raw_resp # Grok/PPLX/LM Studio are already OpenAI-compatible

    @staticmethod
    def extract_reasoning(content: str) -> tuple[str, str]:
        """Extracts DeepSeek reasoning tags <think>...</think> from content."""
        think_match = _THINK_TAG_RE.search(content)
        if think_match:
            reasoning = think_match.group(1).strip()
            final_content = _THINK_TAG_RE.sub("", content).strip()
            return reasoning, final_content
        return "", content

    # ---------- Tool-calling extensions (v2 dispatch loop) ----------

    _GEMINI_ALLOWED_SCHEMA_KEYS = {
        "type", "description", "enum", "properties", "required",
        "items", "format", "nullable", "minimum", "maximum",
        "minItems", "maxItems", "minLength", "maxLength", "pattern",
    }

    @staticmethod
    def _strip_gemini_unsupported(schema):
        """Recursively copy a JSONSchema dict, keeping only Gemini-accepted keys.

        Drops additionalProperties, $schema, default, title, examples, and
        anything else not in _GEMINI_ALLOWED_SCHEMA_KEYS. Descends into
        `properties` (dict of name -> subschema) and `items` (subschema or
        list of subschemas). Non-dict inputs are returned as-is.
        """
        if not isinstance(schema, dict):
            return schema
        cleaned = {}
        for key, value in schema.items():
            if key not in AgentProtocol._GEMINI_ALLOWED_SCHEMA_KEYS:
                continue
            if key == "properties" and isinstance(value, dict):
                cleaned[key] = {
                    pname: AgentProtocol._strip_gemini_unsupported(pschema)
                    for pname, pschema in value.items()
                }
            elif key == "items":
                if isinstance(value, list):
                    cleaned[key] = [AgentProtocol._strip_gemini_unsupported(v) for v in value]
                else:
                    cleaned[key] = AgentProtocol._strip_gemini_unsupported(value)
            else:
                cleaned[key] = value
        return cleaned

    @staticmethod
    def openai_tools_to_anthropic_tools(tools: list) -> list:
        """Convert OpenAI-shape tools list to Anthropic's input_schema shape."""
        if not tools:
            return []
        out = []
        for tool in tools:
            fn = tool.get("function", {})
            out.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return out

    @staticmethod
    def openai_tools_to_gemini_tools(tools: list) -> list:
        """Convert OpenAI-shape tools list to Gemini's functionDeclarations wrapper."""
        if not tools:
            return []
        declarations = []
        for tool in tools:
            fn = tool.get("function", {})
            params = fn.get("parameters", {"type": "object", "properties": {}})
            declarations.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": AgentProtocol._strip_gemini_unsupported(params),
            })
        return [{"functionDeclarations": declarations}]

    @staticmethod
    def _safe_json_loads(raw):
        """Parse a JSON string; fall back to {} on malformed input."""
        if isinstance(raw, dict):
            return raw
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return {}

    @staticmethod
    def openai_to_anthropic_with_tools(messages: list, model: str,
                                        tools: list = None, max_tokens: int = 4096) -> dict:
        """Tool-aware variant of openai_to_anthropic."""
        system = ""
        anthropic_messages = []

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "") or ""

            if role == "system":
                system = content
                continue

            if role == "tool":
                block = {
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content,
                }
                # Merge consecutive tool results into one user message.
                if anthropic_messages and anthropic_messages[-1]["role"] == "user" \
                        and isinstance(anthropic_messages[-1]["content"], list) \
                        and anthropic_messages[-1]["content"] \
                        and anthropic_messages[-1]["content"][0].get("type") == "tool_result":
                    anthropic_messages[-1]["content"].append(block)
                else:
                    anthropic_messages.append({"role": "user", "content": [block]})
                continue

            if role == "assistant":
                tool_calls = msg.get("tool_calls") or []
                if not tool_calls:
                    anthropic_messages.append({"role": "assistant", "content": content})
                    continue
                blocks = []
                if content:  # omit empty text block — Anthropic rejects it
                    blocks.append({"type": "text", "text": content})
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", ""),
                        "name": fn.get("name", ""),
                        "input": AgentProtocol._safe_json_loads(fn.get("arguments", "{}")),
                    })
                anthropic_messages.append({"role": "assistant", "content": blocks})
                continue

            # user or anything else — pass through as plain text
            anthropic_messages.append({"role": "user", "content": content})

        payload = {
            "model": model,
            "messages": anthropic_messages,
            "max_tokens": max_tokens,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = AgentProtocol.openai_tools_to_anthropic_tools(tools)
        return payload

    @staticmethod
    def openai_to_gemini_with_tools(messages: list, model: str, tools: list = None) -> dict:
        """Tool-aware variant of openai_to_gemini."""
        contents = []
        system_instruction = None

        for msg in messages:
            role = msg.get("role")
            content = msg.get("content", "") or ""

            if role == "system":
                system_instruction = {"parts": [{"text": content}]}
                continue

            if role == "tool":
                part = {
                    "functionResponse": {
                        "name": msg.get("name", ""),
                        "response": {"result": content},
                    }
                }
                if contents and contents[-1]["role"] == "user" \
                        and contents[-1]["parts"] \
                        and "functionResponse" in contents[-1]["parts"][0]:
                    contents[-1]["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
                continue

            if role == "assistant":
                tool_calls = msg.get("tool_calls") or []
                parts = []
                if content:
                    parts.append({"text": content})
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    parts.append({
                        "functionCall": {
                            "name": fn.get("name", ""),
                            "args": AgentProtocol._safe_json_loads(fn.get("arguments", "{}")),
                        }
                    })
                if not parts:
                    parts = [{"text": ""}]
                contents.append({"role": "model", "parts": parts})
                continue

            contents.append({"role": "user", "parts": [{"text": content}]})

        payload = {"contents": contents}
        if system_instruction:
            payload["system_instruction"] = system_instruction
        if tools:
            payload["tools"] = AgentProtocol.openai_tools_to_gemini_tools(tools)
        return payload

    @staticmethod
    def parse_tool_calls(raw_response: dict, provider: str) -> tuple:
        """Return (assistant_text, tool_calls_openai_shape) for any provider."""
        if provider in ("openai_compat", "grok", "openai", "lmstudio"):
            choices = raw_response.get("choices") or [{}]
            msg = choices[0].get("message", {}) if choices else {}
            text = msg.get("content") or ""
            tool_calls = msg.get("tool_calls") or []
            return text, list(tool_calls)

        if provider == "anthropic":
            blocks = raw_response.get("content") or []
            text_parts = []
            tool_calls = []
            for block in blocks:
                btype = block.get("type")
                if btype == "text":
                    text_parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })
            return "".join(text_parts), tool_calls

        if provider == "gemini":
            candidates = raw_response.get("candidates") or [{}]
            parts = (candidates[0].get("content") or {}).get("parts") or []
            text_parts = []
            tool_calls = []
            for part in parts:
                if "text" in part and part["text"] is not None:
                    text_parts.append(part["text"])
                if "functionCall" in part:
                    fc = part["functionCall"] or {}
                    tool_calls.append({
                        "id": "call_" + uuid.uuid4().hex[:12],
                        "type": "function",
                        "function": {
                            "name": fc.get("name", ""),
                            "arguments": json.dumps(fc.get("args") or {}),
                        },
                    })
            return "".join(text_parts), tool_calls

        # Unknown provider — best-effort passthrough
        return raw_response.get("content", "") or "", []

    @staticmethod
    def format_tool_result(tool_call_id: str, name: str, result: str,
                           is_error: bool = False) -> dict:
        """Build an OpenAI-shape tool message for the dispatch loop history."""
        content = result if result is not None else ""
        if is_error and not content.startswith("Error: "):
            content = "Error: " + content
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": content,
        }
