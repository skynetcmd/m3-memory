"""doctor files_extraction_probe — reasoning-model detection.

The probe warns when the loaded extraction model reasons (thinking on), because
the answer then goes to the model's reasoning channel instead of JSON content —
extraction is slow and yields ~0 facts. Detection is by OUTPUT (not model name,
which doesn't advertise it) and must recognize LM Studio's `reasoning_content`,
Ollama's `thinking`/`reasoning`, and inline `<think>`. These pin `_classify_
thinking` on those shapes. Pure/hermetic — no server.
"""
import sys
from pathlib import Path

_BIN = str(Path(__file__).resolve().parents[1] / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from doctor import files_extraction_probe as P  # noqa: E402


def _body(message: dict) -> dict:
    return {"choices": [{"message": message}]}


def test_lmstudio_reasoning_content_is_on():
    assert P._classify_thinking(_body({"reasoning_content": "let me think...", "content": ""})) == "on"


def test_ollama_thinking_field_is_on():
    assert P._classify_thinking(_body({"thinking": "hmm", "content": "OK"})) == "on"


def test_reasoning_field_is_on():
    assert P._classify_thinking(_body({"reasoning": "step 1...", "content": ""})) == "on"


def test_inline_think_block_is_on():
    assert P._classify_thinking(_body({"content": "<think>reasoning</think> OK"})) == "on"


def test_plain_answer_is_off():
    assert P._classify_thinking(_body({"content": "OK"})) == "off"


def test_empty_reasoning_is_not_on():
    # A reasoning key present but empty must NOT count as thinking-on.
    assert P._classify_thinking(_body({"reasoning_content": "   ", "content": "OK"})) == "off"


def test_unrecognizable_shape_is_none():
    assert P._classify_thinking({"choices": []}) == "off"          # no message -> plain
    assert P._classify_thinking({"choices": [{"message": "nope"}]}) is None  # message not a dict
