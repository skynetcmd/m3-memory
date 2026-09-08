"""The context-overflow contract, driven against the REAL FastAPI app.

This is the test that would have caught issue #139. The existing coverage
(tests/test_dense_chunk_recovery.py:185) hand-feeds `post_one` a fabricated
error string containing "tokens > n_ctx" -- an assumption the standalone server
never satisfied, because FastAPI's default handler had already replaced the
message with "Internal Server Error". The seam was green in CI while broken in
production for every standalone deployment.

So: no fabricated strings. The server tests drive `embed_server_inproc.app`
through TestClient and stub only the embedder call itself; the client tests feed
`_parse_context_overflow` the bodies a real server actually returns.

Hermetic (§3): no GPU, no model, no network, no live 8082 service.
"""
from __future__ import annotations

import json
import pathlib
import sys
import unittest

_BIN = pathlib.Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(_BIN))

from fastapi.testclient import TestClient  # noqa: E402

import embed_server_inproc as S  # noqa: E402
from memory.embed import ContextLengthExceeded, _parse_context_overflow  # noqa: E402

_OVERFLOW_MSG = "embed failed: backend error: input too long: 16875 tokens > n_ctx 8192"


class _StubEmbed:
    """Swap `_embed` for the duration of a test, restoring it afterwards."""

    def __init__(self, fn):
        self.fn, self._saved = fn, None

    def __enter__(self):
        self._saved = S._embed
        S._embed = self.fn
        return self

    def __exit__(self, *exc):
        S._embed = self._saved
        return False


async def _raise_overflow(texts, interactive=False):
    raise RuntimeError(_OVERFLOW_MSG)


async def _raise_infra(texts, interactive=False):
    raise RuntimeError("embed pool mutex poisoned")


async def _ok(texts, interactive=False):
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


class TestServerReturnsStructured413(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(S.app, raise_server_exceptions=False)

    def test_overflow_becomes_413_not_500(self):
        """The whole bug in one assertion: this used to be a 500 whose body was
        the literal string "Internal Server Error"."""
        with _StubEmbed(_raise_overflow):
            r = self.client.post("/embedding", json={"input": ["x" * 100]})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json()["error"]["code"], "context_length_exceeded")

    def test_413_carries_the_token_counts(self):
        with _StubEmbed(_raise_overflow):
            r = self.client.post("/embedding", json={"input": ["x"]})
        err = r.json()["error"]
        self.assertEqual(err["observed_tokens"], 16875)
        self.assertEqual(err["max_tokens"], 8192)

    def test_openai_compat_endpoint_is_guarded_too(self):
        """/v1/embeddings had the identical unguarded call."""
        with _StubEmbed(_raise_overflow):
            r = self.client.post("/v1/embeddings", json={"input": ["x"]})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json()["error"]["code"], "context_length_exceeded")

    def test_infrastructure_failure_still_500s(self):
        """A poisoned mutex is NOT a client length error. Mislabelling it would
        send an operator to shrink their inputs while the real fault went
        unreported -- the same misdiagnosis as #139, in the other direction."""
        with _StubEmbed(_raise_infra):
            r = self.client.post("/embedding", json={"input": ["x"]})
        self.assertEqual(r.status_code, 500)

    def test_healthy_request_is_untouched(self):
        with _StubEmbed(_ok):
            r = self.client.post("/embedding", json={"input": ["a", "b"]})
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual([d["index"] for d in data], [0, 1])

    def test_old_client_regex_still_matches_the_new_body(self):
        """THE SKEW PROPERTY. Client and server deploy independently here, so
        upgrading the SERVER alone must repair clients already in the field: the
        413 body still contains the original "N tokens > n_ctx M"."""
        import re
        legacy = re.compile(r"(\d+)\s*tokens\s*>\s*n_ctx")
        with _StubEmbed(_raise_overflow):
            r = self.client.post("/embedding", json={"input": ["x"]})
        self.assertIsNotNone(legacy.search(r.text))


class TestClientParsesRealServerBodies(unittest.TestCase):
    """Both skew directions, using bodies a real server actually emits."""

    def test_structured_413_needs_no_string_parsing(self):
        body = json.dumps({"error": {
            "code": "context_length_exceeded",
            "message": _OVERFLOW_MSG,
            "observed_tokens": 16875,
            "max_tokens": 8192,
        }})
        err = _parse_context_overflow(413, body)
        self.assertIsInstance(err, ContextLengthExceeded)
        self.assertEqual((err.observed_tokens, err.max_tokens), (16875, 8192))

    def test_old_server_raw_llama_text(self):
        err = _parse_context_overflow(500, _OVERFLOW_MSG)
        self.assertIsInstance(err, ContextLengthExceeded)
        self.assertEqual(err.observed_tokens, 16875)

    def test_variant_wording_without_counts_is_still_recognised(self):
        """Recovery must depend on knowing the input is too long, not on the
        server having been chatty about numbers."""
        for body in ('{"error":"maximum context length is 8192"}',
                     "token length exceeds limit",
                     "<html>413 Request Entity Too Large: input too long</html>"):
            self.assertIsInstance(
                _parse_context_overflow(413, body), ContextLengthExceeded, body)

    def test_infra_failure_is_not_an_overflow(self):
        self.assertIsNone(_parse_context_overflow(500, "embed pool mutex poisoned"))

    def test_destroyed_body_cannot_be_recovered(self):
        """Honest limit: an OLD, UNPATCHED server still yields "Internal Server
        Error" and the information is genuinely gone. That is exactly why the
        server-side fix is the primary one, not the client-side widening."""
        self.assertIsNone(_parse_context_overflow(500, "Internal Server Error"))

    def test_empty_body(self):
        self.assertIsNone(_parse_context_overflow(500, ""))


class TestSubdivideWithoutCounts(unittest.TestCase):
    def test_zero_observed_tokens_still_splits(self):
        """A server reporting the CLASS of error without a count must not leave
        the chunk unsplit -- that would drop the row."""
        from memory.chunking import _subdivide_dense_chunk
        text = "数据库连接失败" * 3000
        self.assertGreater(len(_subdivide_dense_chunk(text, 0)), 1)


if __name__ == "__main__":
    unittest.main()
