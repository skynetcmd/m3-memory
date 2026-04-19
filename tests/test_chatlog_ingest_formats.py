"""Tests for bin/chatlog_ingest.py — format parsing and ingestion."""

import json
import pytest


def test_parse_claude_code_jsonl():
    """Parse claude-code JSONL format."""
    import chatlog_ingest

    jsonl = """{"type":"message","role":"user","content":"Hello","model":"claude-3-sonnet","conversation_id":"conv-1","usage":{"input_tokens":10,"output_tokens":20}}
{"type":"message","role":"assistant","content":"Hi there","model":"claude-3-sonnet","conversation_id":"conv-1","usage":{"input_tokens":20,"output_tokens":30}}"""

    messages = chatlog_ingest._parse_claude_code(jsonl)

    assert len(messages) == 2
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "Hello"
    assert messages[0]["model_id"] == "claude-3-sonnet"
    assert messages[0]["tokens_in"] == 10
    assert messages[1]["role"] == "assistant"


def test_parse_claude_code_infers_provider():
    """claude-code parser infers anthropic provider."""
    import chatlog_ingest

    jsonl = '{"type":"message","role":"user","content":"Hello","model":"claude-3-sonnet","conversation_id":"conv-1"}'

    messages = chatlog_ingest._parse_claude_code(jsonl)

    assert len(messages) == 1
    assert messages[0]["provider"] == "anthropic"


def test_parse_claude_code_empty_input():
    """Empty input returns empty list."""
    import chatlog_ingest

    messages = chatlog_ingest._parse_claude_code("")

    assert messages == []


def test_parse_claude_code_malformed_json_skipped():
    """Malformed JSON lines are skipped."""
    import chatlog_ingest

    jsonl = """{"type":"message","role":"user","content":"Valid","model":"claude-3-sonnet","conversation_id":"conv-1"}
{invalid json}
{"type":"message","role":"assistant","content":"Also valid","model":"claude-3-sonnet","conversation_id":"conv-1"}"""

    messages = chatlog_ingest._parse_claude_code(jsonl)

    assert len(messages) == 2  # Invalid line skipped


def test_parse_gemini_cli_json():
    """Parse gemini-cli JSON format with history."""
    import chatlog_ingest

    json_data = json.dumps({
        "history": [
            {
                "role": "user",
                "parts": ["What is 2+2?"],
                "model": "gemini-2.5-pro",
            },
            {
                "role": "model",
                "parts": ["4"],
                "model": "gemini-2.5-pro",
            },
        ]
    })

    messages = chatlog_ingest._parse_gemini_cli(json_data)

    assert len(messages) >= 1
    # Exact parsing depends on implementation


def test_parse_gemini_cli_empty_input():
    """Empty gemini-cli input returns empty list."""
    import chatlog_ingest

    messages = chatlog_ingest._parse_gemini_cli("")

    assert messages == []


def test_infer_provider_claude():
    """infer_provider identifies claude models as anthropic."""
    import chatlog_ingest

    assert chatlog_ingest.infer_provider("claude-3-sonnet") == "anthropic"
    assert chatlog_ingest.infer_provider("claude-opus-4-7") == "anthropic"


def test_infer_provider_gemini():
    """infer_provider identifies gemini models as google."""
    import chatlog_ingest

    assert chatlog_ingest.infer_provider("gemini-2.5-pro") == "google"
    assert chatlog_ingest.infer_provider("palm-2") == "google"


def test_infer_provider_gpt():
    """infer_provider identifies gpt models as openai."""
    import chatlog_ingest

    assert chatlog_ingest.infer_provider("gpt-4o") == "openai"
    assert chatlog_ingest.infer_provider("gpt-4.1") == "openai"
    assert chatlog_ingest.infer_provider("o1-preview") == "openai"


def test_infer_provider_grok():
    """infer_provider identifies grok models as xai."""
    import chatlog_ingest

    assert chatlog_ingest.infer_provider("grok-4") == "xai"


def test_infer_provider_deepseek():
    """infer_provider identifies deepseek models."""
    import chatlog_ingest

    assert chatlog_ingest.infer_provider("deepseek-chat") == "deepseek"


def test_infer_provider_unknown():
    """infer_provider returns 'other' for unknown models."""
    import chatlog_ingest

    assert chatlog_ingest.infer_provider("unknown-model") == "other"
    assert chatlog_ingest.infer_provider("") == "other"


def test_infer_provider_llama():
    """infer_provider identifies llama models as local."""
    import chatlog_ingest

    assert chatlog_ingest.infer_provider("llama-2-70b") == "local"
    assert chatlog_ingest.infer_provider("mistral-7b") == "local"
    assert chatlog_ingest.infer_provider("qwen-72b") == "local"


def test_claude_code_missing_fields_skipped():
    """claude-code messages without content/role are skipped."""
    import chatlog_ingest

    jsonl = """{"type":"message","role":"user","content":"Valid","model":"claude-3-sonnet","conversation_id":"conv-1"}
{"type":"message","model":"claude-3-sonnet"}
{"type":"message","role":"assistant","content":"Valid 2","model":"claude-3-sonnet","conversation_id":"conv-1"}"""

    messages = chatlog_ingest._parse_claude_code(jsonl)

    assert len(messages) == 2  # Middle one skipped


def test_parse_claude_code_no_usage():
    """claude-code messages without usage are valid (null tokens)."""
    import chatlog_ingest

    jsonl = '{"type":"message","role":"user","content":"Hello","model":"claude-3-sonnet","conversation_id":"conv-1"}'

    messages = chatlog_ingest._parse_claude_code(jsonl)

    assert len(messages) == 1
    assert messages[0]["tokens_in"] is None
    assert messages[0]["tokens_out"] is None


def test_parse_maintains_conversation_id():
    """Parsed messages maintain conversation_id."""
    import chatlog_ingest

    jsonl = '{"type":"message","role":"user","content":"Hello","model":"claude-3-sonnet","conversation_id":"my-conv-123"}'

    messages = chatlog_ingest._parse_claude_code(jsonl)

    assert messages[0]["conversation_id"] == "my-conv-123"


def test_claude_code_non_message_types_ignored():
    """Non-message type entries are ignored."""
    import chatlog_ingest

    jsonl = """{"type":"system_event","role":"user","content":"Ignored"}
{"type":"message","role":"user","content":"Valid","model":"claude-3-sonnet","conversation_id":"conv-1"}"""

    messages = chatlog_ingest._parse_claude_code(jsonl)

    assert len(messages) == 1
    assert messages[0]["content"] == "Valid"
