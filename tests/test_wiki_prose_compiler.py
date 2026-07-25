"""The concrete LLM ProseCompiler (bin/wiki/prose_compiler.py).

The model call is non-deterministic and network-bound, so it is NOT exercised
here (and stays off the drift-tested path). What IS tested: deterministic prompt
construction, the never-silent truncation note (§3), the output cleaner, fail-open
on a failing/absent endpoint, and — the integration that matters — that
LLMProseCompiler satisfies the ProseCompiler protocol compile.py consumes, so
injecting it drives the real writer end to end (with the HTTP call mocked).
"""
import os
import sys

_HERE = os.path.dirname(__file__)
_BIN = os.path.normpath(os.path.join(_HERE, "..", "bin"))
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

from wiki import compile as WC  # noqa: E402
from wiki import prose_compiler as PC  # noqa: E402
from wiki.cluster import Cluster  # noqa: E402
from wiki.select import Mem  # noqa: E402


def _mem(id, content="body", conf=0.7):
    return Mem(id=id, type="belief", title=f"T-{id}", content=content,
               importance=0.5, confidence=conf, valid_from=None, valid_to=None,
               pinned=0, created_at=None, updated_at=None, metadata={})


def _cluster(*mems):
    ms = list(mems)
    return Cluster(key=min(m.id for m in ms), members=ms)


# ── prompt construction (deterministic) ──────────────────────────────────────

def test_prompt_is_deterministic():
    c = _cluster(_mem("a", "alpha content"), _mem("b", "beta content"))
    cfg = PC.ProseCompilerConfig()
    p1 = PC._user_prompt(c, cfg)
    p2 = PC._user_prompt(c, cfg)
    assert p1 == p2
    assert "alpha content" in p1 and "beta content" in p1
    assert p1.startswith("Topic: ")


def test_prompt_truncation_is_never_silent():
    """A cluster larger than max_members must SAY so in the prompt (§3) — a
    silently truncated prompt would compile a page from a subset with no record."""
    cfg = PC.ProseCompilerConfig(max_members=2)
    c = _cluster(_mem("a"), _mem("b"), _mem("c"), _mem("d"))
    p = PC._user_prompt(c, cfg)
    assert "2 further note(s) omitted" in p


def test_prompt_snippet_is_bounded():
    cfg = PC.ProseCompilerConfig(snippet_chars=10)
    c = _cluster(_mem("a", "x" * 200))
    p = PC._user_prompt(c, cfg)
    assert "x" * 200 not in p          # full body not dumped
    assert "…" in p                    # truncation marker present


# ── output cleaner ───────────────────────────────────────────────────────────

def test_clean_strips_a_wrapping_code_fence():
    assert PC._clean("```markdown\n# Page\nbody\n```") == "# Page\nbody"


def test_clean_leaves_plain_prose():
    assert PC._clean("  # Page\nbody  ") == "# Page\nbody"


def test_clean_handles_empty():
    assert PC._clean("") == ""
    assert PC._clean(None) == ""


# ── fail-open ────────────────────────────────────────────────────────────────

def test_compile_returns_none_when_endpoint_unreachable(monkeypatch):
    """A dead endpoint → None, never an exception. compile.py maps None to
    'compile-failed' and continues the batch."""
    monkeypatch.setattr(PC, "_call_model", lambda cfg, prompt: None)
    comp = PC.LLMProseCompiler()
    assert comp.compile(_cluster(_mem("a"))) is None
    assert comp.failures == 1


def test_compile_returns_prose_on_success(monkeypatch):
    monkeypatch.setattr(PC, "_call_model", lambda cfg, prompt: "COMPILED PAGE")
    comp = PC.LLMProseCompiler()
    assert comp.compile(_cluster(_mem("a"))) == "COMPILED PAGE"


# ── protocol integration: it drives the real writer ─────────────────────────

def test_satisfies_prosecompiler_protocol():
    """Structural check: the attributes/methods compile.py relies on exist."""
    comp = PC.LLMProseCompiler()
    assert isinstance(comp.prompt_version, str) and comp.prompt_version
    assert isinstance(comp.model, str)
    assert isinstance(comp.prompt_text(), str) and comp.prompt_text()
    assert callable(comp.compile)


def test_prompt_text_records_the_version_for_audit():
    comp = PC.LLMProseCompiler()
    txt = comp.prompt_text()
    assert f"prompt_version={comp.prompt_version}" in txt
    assert "SYSTEM:" in txt  # the actual instruction is in the audit record


import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_drives_the_compile_writer_end_to_end(monkeypatch):
    """Inject the real compiler (HTTP mocked) into compile_cluster and confirm a
    synthesis is written with the compiler's prose — the two halves connect."""
    monkeypatch.setattr(PC, "_call_model",
                        lambda cfg, prompt: "# Alpha\n\nSynthesized page.")
    comp = PC.LLMProseCompiler()
    c = _cluster(_mem("a", conf=0.9), _mem("b", conf=0.5))

    writes = []

    async def _write(**a):
        writes.append(a)
        return "Created: syn-1"

    stats = WC.CompileStats()
    r = await WC.compile_cluster(c, comp, head=None, prompt_memory_id="p-1",
                                 stats=stats, _write=_write)
    assert r.action == "created"
    assert len(writes) == 1
    assert writes[0]["type"] == "synthesis"
    assert writes[0]["content"] == "# Alpha\n\nSynthesized page."
    assert writes[0]["confidence"] == 0.5   # MIN of member confidences
