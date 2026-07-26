"""Model selection must not need a model NAME — and must never guess one.

USER DIRECTIVE (2026-07-26): "when we're using SLM/LLM, we should allow for
model selection but allow no naming of model too."

The seam already existed (llm_failover.get_best_llm, async). What was missing
was a SYNC sibling, so synchronous callers had each hardcoded a model name as
their DEFAULT — configurable via env, but the FALLBACK named a model. On a box
without that exact model that is a 404 from the server rather than a graceful
fall-through to discovery.

Precedence these tests pin, everywhere:

    explicit override  >  discovery  >  None (fail loud)

A hardcoded literal must never be the tail. `None` means "caller decides and
says so"; it must not be turned back into a name.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BIN = str(Path(__file__).resolve().parents[1] / "bin")
if _BIN not in sys.path:
    sys.path.insert(0, _BIN)

import llm_failover as L  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    """Discovery caches per-endpoint; a leaked entry would fake a pass."""
    L.clear_failover_caches()
    for var in (
        "M3_FILES_SUMMARY_MODEL",
        "M3_FILES_EXTRACT_MODEL",
        "M3_FILES_SUMMARY_URL",
        "M3_LMSTUDIO_URL",
        "LM_API_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    yield
    L.clear_failover_caches()


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._p


def _fake_httpx(monkeypatch, payload, *, capture=None, status=200):
    """Patch httpx.get as imported INSIDE discover_model_sync."""
    import httpx

    def _get(url, headers=None, timeout=None):
        if capture is not None:
            capture["url"], capture["headers"] = url, headers
        return _Resp(payload, status)

    monkeypatch.setattr(httpx, "get", _get)


def _strip_comments_and_docstrings(src: str) -> str:
    """Return executable source only — no comments, no docstrings.

    Both are legitimate places to NAME the removed literal (this change
    documents what it removed and why). Only a live default is the defect, so
    drop comments and docstrings before grepping.
    """
    import ast

    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            body[0].value.value = ""
    # ast.unparse drops comments for free — they are not in the tree.
    return ast.unparse(tree)


class TestDiscoverModelSync:
    def test_picks_largest_and_needs_no_name(self, monkeypatch):
        _fake_httpx(monkeypatch, {"data": [{"id": "qwen3-4b"}, {"id": "qwen3-14b"}]})
        assert L.discover_model_sync("http://x/v1") == "qwen3-14b"

    def test_excludes_embedding_models(self, monkeypatch):
        _fake_httpx(
            monkeypatch,
            {"data": [{"id": "text-embedding-nomic-embed-text-v1.5"}, {"id": "qwen3-4b"}]},
        )
        # The embedder is "larger" by no measure, but it must be filtered on
        # identity, not size — an LLM caller must never receive an embedder.
        assert L.discover_model_sync("http://x/v1") == "qwen3-4b"

    def test_ollama_native_models_key(self, monkeypatch):
        """Ollama's native listing uses {"models": [...]}, not {"data": [...]}.

        The /v1 shim returns "data"; accepting both means an Ollama user is not
        silently left with no model. Ollama support is otherwise UNVERIFIED —
        see m3 to-do 79772da4.
        """
        _fake_httpx(monkeypatch, {"models": [{"model": "qwen3:4b"}]})
        assert L.discover_model_sync("http://x/v1") == "qwen3:4b"

    def test_unreachable_returns_none_not_a_guess(self, monkeypatch):
        import httpx

        def _boom(*a, **k):
            raise httpx.ConnectError("refused")

        monkeypatch.setattr(httpx, "get", _boom)
        assert L.discover_model_sync("http://x/v1") is None

    def test_empty_list_returns_none_not_a_guess(self, monkeypatch):
        _fake_httpx(monkeypatch, {"data": []})
        assert L.discover_model_sync("http://x/v1") is None

    def test_auth_header_omitted_when_no_token(self, monkeypatch):
        """A junk bearer is NOT equivalent to no bearer.

        Sending "not-needed" earned a 401 from the live LM Studio on
        2026-07-26; an auth-enabled server answers a MISSING header differently
        from a WRONG one. Mirrors files_memory.config.llm_auth_headers.
        """
        cap: dict = {}
        _fake_httpx(monkeypatch, {"data": [{"id": "m"}]}, capture=cap)
        L.discover_model_sync("http://x/v1")
        assert cap["headers"] == {}

    def test_auth_header_sent_when_token_present(self, monkeypatch):
        cap: dict = {}
        monkeypatch.setenv("LM_API_TOKEN", "tok123")
        _fake_httpx(monkeypatch, {"data": [{"id": "m"}]}, capture=cap)
        L.discover_model_sync("http://x/v1")
        assert cap["headers"] == {"Authorization": "Bearer tok123"}

    def test_caches_per_endpoint_not_globally(self, monkeypatch):
        """Sync callers resolve their OWN endpoint; a global cache would hand
        back a model that the endpoint they POST to may not serve."""
        _fake_httpx(monkeypatch, {"data": [{"id": "model-a"}]})
        assert L.discover_model_sync("http://a/v1") == "model-a"
        _fake_httpx(monkeypatch, {"data": [{"id": "model-b"}]})
        assert L.discover_model_sync("http://b/v1") == "model-b"
        # ...and 'a' still returns its own cached answer, not b's.
        assert L.discover_model_sync("http://a/v1") == "model-a"


class TestCallersDoNotHardcodeNames:
    """The regression: no caller may name a model as its FALLBACK."""

    def test_summary_model_prefers_explicit_override(self, monkeypatch):
        from files_memory import summarize

        monkeypatch.setenv("M3_FILES_SUMMARY_URL", "http://x/v1")
        monkeypatch.setenv("M3_FILES_SUMMARY_MODEL", "pinned-x")
        assert summarize._summary_model() == "pinned-x"

    def test_summary_model_falls_back_to_discovery(self, monkeypatch):
        from files_memory import summarize

        monkeypatch.setenv("M3_FILES_SUMMARY_URL", "http://x/v1")
        _fake_httpx(monkeypatch, {"data": [{"id": "discovered-model"}]})
        assert summarize._summary_model() == "discovered-model"

    def test_summary_model_returns_none_when_discovery_fails(self, monkeypatch):
        from files_memory import summarize

        monkeypatch.setenv("M3_FILES_SUMMARY_URL", "http://x/v1")
        _fake_httpx(monkeypatch, {"data": []})
        assert summarize._summary_model() is None, "must not guess a model name"

    def test_extract_model_returns_none_when_discovery_fails(self, monkeypatch):
        from files_memory import extract

        monkeypatch.setenv("M3_FILES_SUMMARY_URL", "http://x/v1")
        _fake_httpx(monkeypatch, {"data": []})
        assert extract._llm_model() is None, "must not guess a model name"

    @pytest.mark.parametrize(
        "module,literal",
        [
            ("files_memory/summarize.py", "qwen3-4b-instruct"),
            ("files_memory/extract.py", "qwen3-4b-instruct"),
            ("debug_agent_bridge.py", "text-embedding-nomic-embed-text-v1.5"),
        ],
    )
    def test_no_model_literal_survives_as_a_default(self, module, literal):
        """Grep-level guard: the literals are gone from these modules.

        Deliberately textual. The behavioural tests above cover the wired paths;
        this catches a literal creeping back into a path no test exercises,
        which is exactly how these accumulated.
        """
        src = (Path(_BIN) / module).read_text(encoding="utf-8")
        code = _strip_comments_and_docstrings(src)
        assert literal not in code, (
            f"{module} still names {literal!r} outside a comment — "
            "model names belong in an override, never a fallback"
        )
