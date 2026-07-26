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

    CAUTION: ast.unparse normalises string QUOTES to single quotes, so a
    double-quoted needle will never match the output. Callers must search
    quote-agnostically — see _contains_literal. A guard that cannot fail is
    worse than no guard, and this one silently passed against a deliberately
    reverted call site until that was found.
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


def _contains_literal(code: str, needle: str) -> bool:
    """Quote-agnostic substring search over ast.unparse output.

    ast.unparse re-emits every string with SINGLE quotes, so searching for
    '"/v1/chat/completions"' against it always fails. Normalise both sides.
    """
    return needle.replace('"', "'") in code.replace('"', "'")



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
        assert not _contains_literal(code, literal), (
            f"{module} still names {literal!r} outside a comment — "
            "model names belong in an override, never a fallback"
        )


class TestAuthHeaderOmittedWhenTokenless:
    """`Bearer ` with an empty token is an ILLEGAL HTTP header value.

    REGRESSION (found 2026-07-26 against a live Ollama). get_best_llm,
    get_smallest_llm and get_best_embed each built
    `headers={"Authorization": f"Bearer {token}"}` unconditionally. With no
    token that is the literal string "Bearer ", which httpx rejects with
    LocalProtocolError BEFORE the request leaves the process. All three wrap
    discovery in a broad `except Exception`, so the failure surfaced as
    "endpoint unusable" — get_best_llm returned None against a perfectly
    healthy server, and EVERY tokenless endpoint (Ollama's default, an
    auth-disabled LM Studio) was silently undiscoverable.

    Not a hypothetical: this is why the first live-Ollama run of the async
    failover path returned None for all three functions while the sync
    discover_model_sync succeeded on the same endpoint.
    """

    def test_empty_token_omits_the_header(self):
        assert L._auth_headers("") == {}
        assert L._auth_headers(None) == {}
        assert L._auth_headers("   ") == {}, "whitespace-only is still no token"

    def test_real_token_is_sent(self):
        assert L._auth_headers("tok") == {"Authorization": "Bearer tok"}
        assert L._auth_headers("  tok  ") == {"Authorization": "Bearer tok"}

    def test_empty_bearer_is_rejected_by_the_transport(self):
        """Pin WHY the empty header must be omitted rather than sent blank.

        `Bearer ` is an illegal HTTP header VALUE. Crucially the rejection is
        late: httpx.Request(...) and httpx.Headers(...) both accept it without
        complaint, and only the transport raises LocalProtocolError once a
        request is actually sent. That lateness is what made the bug invisible
        — nothing fails until a real request goes out, and there the callers'
        broad `except Exception` reinterprets it as "endpoint unreachable".

        Driven through a local socket so the assertion does not depend on any
        service being up.
        """
        import socket
        import threading

        import httpx

        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]

        def _accept():
            try:
                c, _ = srv.accept()
                c.close()
            except OSError:
                pass

        t = threading.Thread(target=_accept, daemon=True)
        t.start()
        try:
            with pytest.raises(httpx.LocalProtocolError):
                httpx.get(
                    f"http://127.0.0.1:{port}/v1/models",
                    headers={"Authorization": "Bearer "},
                    timeout=5,
                )
        finally:
            srv.close()
            t.join(timeout=2)

    @pytest.mark.parametrize(
        "fn", ["get_best_llm", "get_smallest_llm", "get_best_embed"],
    )
    def test_async_discovery_uses_the_helper(self, fn):
        """All three async paths must route through _auth_headers.

        Textual because the bug was three hand-maintained copies of the same
        header expression; a behavioural test on one would not have caught the
        other two.
        """
        import inspect

        src = inspect.getsource(getattr(L, fn))
        assert "_auth_headers(token)" in src, f"{fn} builds its own auth header"
        assert 'f"Bearer {token}"' not in src, f"{fn} still inlines the header"


class TestPrewarm:
    """Pay the model load cost once, before a batch — not on the first file.

    MEASURED 2026-07-26 against a live Ollama: a generative model's first
    request after idle took 27.05s versus 0.13s warm (209x), against a 30.0s
    per-call timeout in summarize._llm_call — a ~3 second margin. On timeout
    _llm_call returns None, so the file is silently summarised WITHOUT the LLM
    and nothing explains why.

    Ollama makes this reachable in normal use: /v1/models lists INSTALLED
    models, not LOADED ones (verified: 0 loaded while 2 listed), so discovery
    selects a model that is not resident. LM Studio serves what is already
    loaded, which is why this never bit there.

    Verified end-to-end against live Ollama 0.32.4: 0 models loaded ->
    prewarm() 12.67s -> 1 loaded -> first real call 0.34s.
    """

    def _env(self, monkeypatch):
        monkeypatch.setenv("M3_FILES_SUMMARY_URL", "http://x")
        monkeypatch.setenv("M3_FILES_SUMMARY_MODEL", "m")

    def test_returns_false_without_an_endpoint(self, monkeypatch):
        from files_memory import summarize

        monkeypatch.delenv("M3_FILES_SUMMARY_URL", raising=False)
        monkeypatch.delenv("M3_LMSTUDIO_URL", raising=False)
        assert summarize.prewarm() is False

    def test_returns_false_when_no_model_resolves(self, monkeypatch):
        """No model => nothing to warm. Must not POST {"model": null}."""
        from files_memory import summarize

        monkeypatch.setenv("M3_FILES_SUMMARY_URL", "http://x")
        monkeypatch.delenv("M3_FILES_SUMMARY_MODEL", raising=False)
        _fake_httpx(monkeypatch, {"data": []})  # discovery finds nothing
        assert summarize.prewarm() is False

    def test_posts_max_tokens_one_to_the_resolved_model(self, monkeypatch):
        """The cost being paid is the LOAD, not the decode."""
        from files_memory import summarize

        self._env(monkeypatch)
        cap: dict = {}

        class _C:
            def __init__(self, *a, **k):
                cap["timeout"] = k.get("timeout")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, url, headers=None, json=None):
                cap["url"], cap["json"] = url, json
                return _Resp({"choices": [{"message": {"content": "x"}}]})

        import httpx

        monkeypatch.setattr(httpx, "Client", _C)
        assert summarize.prewarm() is True
        assert cap["json"]["model"] == "m"
        assert cap["json"]["max_tokens"] == 1
        # Endpoint convention in files_memory: the base does NOT carry /v1,
        # the caller appends it. Getting this wrong yields /v1/v1/... and a 404.
        assert cap["url"] == "http://x/v1/chat/completions"

    def test_uses_a_generous_separate_timeout(self, monkeypatch):
        """Pre-warm is EXPECTED to be slow; it must not inherit the 30s
        per-call timeout it exists to protect."""
        from files_memory import summarize

        self._env(monkeypatch)
        cap: dict = {}

        class _C:
            def __init__(self, *a, **k):
                cap["timeout"] = k.get("timeout")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                return _Resp({"ok": 1})

        import httpx

        monkeypatch.setattr(httpx, "Client", _C)
        summarize.prewarm()
        assert cap["timeout"] >= 60, "must exceed the 30s per-call timeout"

    def test_never_raises_so_a_batch_is_never_gated_on_it(self, monkeypatch):
        """Non-fatal by contract: an unwarmed batch still runs, exactly as it
        did before pre-warming existed."""
        from files_memory import summarize

        self._env(monkeypatch)

        class _Boom:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                raise RuntimeError("server on fire")

        import httpx

        monkeypatch.setattr(httpx, "Client", _Boom)
        assert summarize.prewarm() is False  # not an exception

    def test_non_200_is_false_not_an_exception(self, monkeypatch):
        from files_memory import summarize

        self._env(monkeypatch)

        class _C:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def post(self, *a, **k):
                return _Resp({"error": "nope"}, status=503)

        import httpx

        monkeypatch.setattr(httpx, "Client", _C)
        assert summarize.prewarm() is False


class TestChatCompletionsUrl:
    """One URL convention, tolerant of both forms users actually have.

    BUG (fixed 2026-07-26). files_memory built its URL as
    ``endpoint.rstrip("/") + "/v1/chat/completions"``, so its base had to OMIT
    /v1 — while everywhere ELSE in the codebase the base INCLUDES it
    (llm_failover._LMSTUDIO_ENDPOINT / _OLLAMA_ENDPOINT,
    m3_core.runtime.LM_STUDIO_BASE, mcp_proxy.LMSTUDIO_BASE, and the documented
    M3_LLM_URL examples). Pasting the same base URL you give LM Studio or
    Ollama into M3_FILES_SUMMARY_URL yielded /v1/v1/chat/completions and a 404.

    The failure was QUIET: _llm_call returns None on any error, so the file was
    simply summarised without the LLM and nothing said why. Hit while testing
    pre-warm — the 404 only became visible because the pre-warm logs it.

    Fixed by accepting BOTH rather than picking a winner and breaking whichever
    config a user already has. Verified live against Ollama: all three base
    forms return a real summary.
    """

    @pytest.mark.parametrize(
        "base",
        [
            "http://localhost:1234",
            "http://localhost:1234/",
            "http://localhost:1234/v1",
            "http://localhost:1234/v1/",
        ],
    )
    def test_all_base_forms_normalise_identically(self, base):
        from files_memory.config import chat_completions_url

        assert chat_completions_url(base) == "http://localhost:1234/v1/chat/completions"

    def test_never_doubles_the_v1_segment(self):
        """The actual defect, stated directly."""
        from files_memory.config import chat_completions_url

        for base in ("http://h/v1", "http://h/v1/", "http://h"):
            assert "/v1/v1/" not in chat_completions_url(base)

    def test_preserves_a_path_prefix(self):
        """A reverse-proxied deployment may live under a subpath; only a
        TRAILING /v1 is the version segment we own."""
        from files_memory.config import chat_completions_url

        assert (
            chat_completions_url("http://h/openai/v1")
            == "http://h/openai/v1/chat/completions"
        )
        # ...and a path segment that merely CONTAINS v1 is not stripped.
        assert (
            chat_completions_url("http://h/v1beta")
            == "http://h/v1beta/v1/chat/completions"
        )

    @pytest.mark.parametrize(
        "module", ["summarize", "extract", "carry_forward"],
    )
    def test_no_caller_hand_builds_the_url(self, module):
        """All three LLM callers must route through the one builder.

        Textual because the bug was four hand-maintained copies of the same
        concatenation; a behavioural test on one would not catch the others.
        """
        src = (Path(_BIN) / "files_memory" / f"{module}.py").read_text(encoding="utf-8")
        code = _strip_comments_and_docstrings(src)
        assert not _contains_literal(code, '"/v1/chat/completions"'), (
            f"{module}.py still hand-builds the chat URL; use "
            "config.chat_completions_url so both base forms work"
        )
