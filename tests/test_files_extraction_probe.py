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


# ── suppression-awareness: the probe mirrors extraction's reasoning_effort ─────

def test_suppressible_matches_local_runtimes():
    # delegates to the shared gate: LM Studio + Ollama yes, others no
    assert P._suppressible("http://localhost:1234/v1") is True
    assert P._suppressible("http://localhost:11434/v1") is True
    assert P._suppressible("http://localhost:8000/v1") is False
    assert P._suppressible("https://api.openai.com/v1") is False


def _capture_detect_payload(monkeypatch, **kwargs):
    """Run _detect_thinking with urlopen stubbed; return the JSON body it POSTed."""
    import io
    import json
    captured = {}

    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(req, timeout=None):  # noqa: ARG001
        captured["body"] = json.loads(req.data.decode())
        return _Resp(json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode())

    monkeypatch.setattr(P.urllib.request, "urlopen", fake_urlopen)
    P._detect_thinking("http://localhost:1234/v1", "m", {}, **kwargs)
    return captured["body"]


def test_detect_thinking_sends_reasoning_effort_when_asked(monkeypatch):
    body = _capture_detect_payload(monkeypatch, reasoning_effort="none")
    assert body.get("reasoning_effort") == "none"


def test_detect_thinking_omits_reasoning_effort_by_default(monkeypatch):
    body = _capture_detect_payload(monkeypatch)
    assert "reasoning_effort" not in body
