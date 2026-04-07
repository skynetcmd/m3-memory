import re

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
        think_match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
        if think_match:
            reasoning = think_match.group(1).strip()
            final_content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
            return reasoning, final_content
        return "", content
