"""Contract tests for the shared in-process GPU embedder server.

The server owns ONE CUDA context (m3_core_rs.EmbeddedEmbedder) and serves it
over localhost HTTP so the MCP server + cognitive loop don't each load their
own (~18 GB -> ~9-10 GB). These tests mock the embedder (CI has no GPU) and
assert the HTTP CONTRACT the client cascade depends on:
  - POST /embedding {"input":[...]} -> {"data":[{"index","embedding"}]} in order.
    `index` is MANDATORY — memory.chunking._order_embeddings rejects a response
    without a complete index permutation (a server omitting it would let
    mis-ordered vectors poison the store). Regression for that exact bug.
  - binary fast-path (Accept: octet-stream) -> f32 body with an 8-byte header.
  - oversized batch -> 413 (fail-loud, never silent truncation, §3).
"""
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import embed_server_inproc as S  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class _FakeEmb:
    def embed(self, texts):
        # deterministic 4-dim vectors, distinct per input
        return [[float(i) + 0.5] * 4 for i, _ in enumerate(texts)]

    def embedding_dim(self):
        return 4


def _client():
    S._embedder = _FakeEmb()
    S._dim = 4
    S._model_tag = "test-bge"
    S._MAX_BATCH = 2048
    return TestClient(S.app)


def test_embedding_batch_has_index_permutation():
    c = _client()
    r = c.post("/embedding", json={"input": ["a", "b", "c"]})
    assert r.status_code == 200
    data = r.json()["data"]
    assert len(data) == 3
    # index MUST be present and a clean permutation (the client rejects otherwise)
    assert [d["index"] for d in data] == [0, 1, 2]
    assert all(len(d["embedding"]) == 4 for d in data)


def test_embedding_binary_fastpath():
    c = _client()
    r = c.post("/embedding", json={"input": ["a", "b"]},
               headers={"Accept": "application/octet-stream"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    n, dim = struct.unpack("<II", r.content[:8])
    assert (n, dim) == (2, 4)
    assert len(r.content) == 8 + n * dim * 4  # header + f32 payload


def test_v1_embeddings_openai_shape():
    c = _client()
    r = c.post("/v1/embeddings", json={"model": "x", "input": "hello"})
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "list"
    assert body["data"][0]["index"] == 0
    assert len(body["data"][0]["embedding"]) == 4


def test_oversized_batch_fails_loud():
    c = _client()
    S._MAX_BATCH = 2
    r = c.post("/embedding", json={"input": ["a", "b", "c"]})
    assert r.status_code == 413  # never silently truncate


def test_empty_input_is_empty_list_not_error():
    c = _client()
    r = c.post("/embedding", json={"input": []})
    assert r.status_code == 200
    assert r.json() == {"data": []}


def test_health_structured():
    c = _client()
    h = c.get("/health").json()
    assert h["status"] == "ok" and h["dim"] == 4 and h["model"] == "test-bge"
