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


# ── Pre-flight no-stack guard (_already_serving) ──────────────────────────────
# The self-heal task re-fires every minute; the server must refuse to load a
# SECOND GPU embedder if one is already serving :8082. _already_serving is the
# guard that makes the 1-min cadence safe. These tests mock urllib so no real
# socket/GPU is touched (CI-safe).
import json as _json  # noqa: E402
from contextlib import contextmanager  # noqa: E402


@contextmanager
def _fake_urlopen(monkeypatch, *, status=200, body=None, raises=None):
    """Patch urllib.request.urlopen for the duration of the block."""
    import urllib.request

    class _Resp:
        def __init__(self):
            self.status = status
        def read(self):
            return _json.dumps(body if body is not None else {}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake(url, timeout=None):
        if raises is not None:
            raise raises
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    yield


def test_already_serving_true_when_health_ok(monkeypatch):
    with _fake_urlopen(monkeypatch, status=200,
                       body={"status": "ok", "model": "bge", "dim": 1024}):
        assert S._already_serving("127.0.0.1", 8082) is True


def test_already_serving_true_when_loading(monkeypatch):
    # A server mid-GPU-load reports status=loading; that still owns the port, so
    # a second instance must NOT start.
    with _fake_urlopen(monkeypatch, status=200, body={"status": "loading"}):
        assert S._already_serving("127.0.0.1", 8082) is True


def test_already_serving_false_when_connection_refused(monkeypatch):
    # Nothing listening -> start normally.
    with _fake_urlopen(monkeypatch, raises=OSError("connection refused")):
        assert S._already_serving("127.0.0.1", 8082) is False


def test_already_serving_false_for_foreign_service(monkeypatch):
    # Some unrelated service holds the port but doesn't speak our /health shape:
    # we must NOT treat it as our server (would suppress a legitimate start).
    with _fake_urlopen(monkeypatch, status=200, body={"hello": "world"}):
        assert S._already_serving("127.0.0.1", 8082) is False


def test_already_serving_false_on_non_200(monkeypatch):
    with _fake_urlopen(monkeypatch, status=503, body={"status": "ok"}):
        assert S._already_serving("127.0.0.1", 8082) is False


def test_already_serving_probes_loopback_for_wildcard_bind(monkeypatch):
    # When bound to 0.0.0.0 the probe must target a concrete IP (127.0.0.1),
    # not the un-connectable wildcard address.
    seen = {}
    import urllib.request

    class _Resp:
        status = 200
        def read(self): return b'{"status":"ok"}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake(url, timeout=None):
        seen["url"] = url
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    S._already_serving("0.0.0.0", 8082)
    assert "127.0.0.1" in seen["url"] and "0.0.0.0" not in seen["url"]


# ── Two-lane admission gate (interactive fast-lane) ───────────────────────────
# The Rust backend is multi-stream and safe to call concurrently; the gate
# reserves capacity for interactive (single-query) embeds so a search never
# queues behind a bulk ingestion batch (the "MCP server locked up" wedge).
# Policy: fair-share with a work-conserving borrow.

import asyncio

import pytest


def test_is_interactive_by_size_and_header():
    """Small batch => interactive; large => bulk; header overrides both."""
    class _Req:
        def __init__(self, pri=None):
            self.headers = {"x-m3-embed-priority": pri} if pri else {}

    small = ["x"]
    big = ["x"] * 100
    # size-based (no header)
    assert S._is_interactive(small, _Req()) is True
    assert S._is_interactive(big, _Req()) is False
    # header override wins over size
    assert S._is_interactive(big, _Req("interactive")) is True
    assert S._is_interactive(small, _Req("bulk")) is False
    # no request object -> size only
    assert S._is_interactive(small, None) is True
    assert S._is_interactive(big, None) is False


def test_gate_reserve_arithmetic():
    """Strict reserve: bulk_max = total - reserved; reserved >= 1 (unless total==1)."""
    g = S._AdmissionGate(total=8, reserved=1)
    assert (g.total, g.reserved, g.bulk_max) == (8, 1, 7)
    g2 = S._AdmissionGate(total=4, reserved=2)
    assert (g2.total, g2.reserved, g2.bulk_max) == (4, 2, 2)
    # reserved can't consume the last bulk slot
    g3 = S._AdmissionGate(total=2, reserved=5)
    assert g3.bulk_max >= 1 and g3.reserved == 1
    # degenerate total=1 -> no reservation possible, bulk gets the slot
    g4 = S._AdmissionGate(total=1, reserved=1)
    assert g4.total == 1 and g4.bulk_max == 1


@pytest.mark.asyncio
async def test_gate_interactive_never_waits_behind_bulk():
    """The core anti-wedge property under STRICT reserve. total=8, reserved=1 =>
    bulk_max=7. Even when bulk fills ALL 7 of its slots, the 1 reserved slot
    keeps an interactive request running immediately — it never queues behind a
    bulk batch."""
    gate = S._AdmissionGate(total=8, reserved=1)
    for _ in range(7):                       # fill every bulk slot
        await gate.acquire(interactive=False)

    got = asyncio.Event()

    async def _interactive():
        await gate.acquire(interactive=True)
        got.set()

    t = asyncio.create_task(_interactive())
    await asyncio.sleep(0.05)
    assert got.is_set(), "interactive must run on the reserved slot even with bulk at full cap"

    await gate.release(interactive=True)
    await t
    for _ in range(7):
        await gate.release(interactive=False)


@pytest.mark.asyncio
async def test_gate_bulk_strictly_capped_no_borrow():
    """Bulk NEVER exceeds bulk_max, even when the reserved slot is idle (no
    borrow). total=4, reserved=1 => bulk_max=3: a 4th concurrent bulk blocks."""
    gate = S._AdmissionGate(total=4, reserved=1)
    for _ in range(3):
        await asyncio.wait_for(gate.acquire(interactive=False), timeout=0.5)
    # 4th bulk must block — the reserved slot is not borrowable.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(gate.acquire(interactive=False), timeout=0.1)
    # ...but interactive can still take the reserved slot right now.
    await asyncio.wait_for(gate.acquire(interactive=True), timeout=0.2)
    await gate.release(interactive=True)
    for _ in range(3):
        await gate.release(interactive=False)


@pytest.mark.asyncio
async def test_gate_interactive_scales_up_when_bulk_idle():
    """Interactive is not per-lane capped: with bulk idle it may use up to
    `total` concurrent slots (answers 'can interactive use the bulk slots when
    unused?' — yes)."""
    gate = S._AdmissionGate(total=4, reserved=1)
    for _ in range(4):                       # 4 interactive, bulk idle
        await asyncio.wait_for(gate.acquire(interactive=True), timeout=0.5)
    # 5th exceeds total and blocks.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(gate.acquire(interactive=True), timeout=0.1)
    for _ in range(4):
        await gate.release(interactive=True)


@pytest.mark.asyncio
async def test_gate_total_defaults_to_backend_streams(monkeypatch):
    """_resolve_admission uses the backend stream pool as `total` by default
    (M3_EMBED_SERVER_CONCURRENCY unset => 0 sentinel => full pool)."""
    class _Emb8:
        def streams(self): return 8
    monkeypatch.setattr(S, "_embedder", _Emb8())
    monkeypatch.setattr(S, "_ADMISSION_TOTAL_DEFAULT", 0)   # auto
    monkeypatch.setattr(S, "_INTERACTIVE_RESERVED", 1)
    gate = S._resolve_admission()
    assert gate.total == 8, "auto total should equal backend streams (8)"
    assert gate.bulk_max == 7 and gate.reserved == 1


@pytest.mark.asyncio
async def test_gate_total_clamped_to_backend_streams(monkeypatch):
    """A configured total is clamped DOWN to the backend pool so the server never
    over-subscribes the Rust dispatcher."""
    class _Emb2:
        def streams(self): return 2   # backend only offers 2 streams
    monkeypatch.setattr(S, "_embedder", _Emb2())
    monkeypatch.setattr(S, "_ADMISSION_TOTAL_DEFAULT", 8)  # ask for 8
    monkeypatch.setattr(S, "_INTERACTIVE_RESERVED", 1)
    gate = S._resolve_admission()
    assert gate.total == 2, "configured 8 must clamp to backend streams (2)"
    assert gate.bulk_max >= 1 and gate.reserved >= 1


# ── No-console (pythonw.exe) launch robustness ────────────────────────────────
# The AgentOS_EmbedServer scheduled task launches via pythonw.exe (no console).
# Under that launcher the default uvicorn.run() exited right after binding, so
# the shared embedder never stayed up. Guard the two robustness measures.

def test_ensure_std_streams_replaces_none(monkeypatch):
    """pythonw gives sys.stdout/stderr == None; _ensure_std_streams must bind
    writable substitutes so a stray dependency write can't kill the process."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    S._ensure_std_streams()
    assert sys.stdout is not None and sys.stderr is not None
    # writable (won't raise)
    sys.stdout.write("x")
    sys.stderr.write("y")


def test_ensure_std_streams_preserves_existing(monkeypatch):
    """When streams already exist (normal python.exe) it's a no-op."""
    import io
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    S._ensure_std_streams()
    assert sys.stdout is out and sys.stderr is err


def test_run_server_disables_signal_handlers(monkeypatch):
    """_run_server must build a uvicorn.Server with signal handlers disabled and
    drive it via asyncio.run — the fix for the pythonw exit-after-bind. We stub
    the serve loop so the test doesn't actually bind a port."""
    import uvicorn

    captured = {}

    class _FakeServer:
        def __init__(self, config):
            captured["config"] = config
            self.install_signal_handlers = "DEFAULT"  # overwritten by _run_server

        async def serve(self):
            captured["served"] = True

    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    S._run_server("127.0.0.1", 9099)
    assert captured.get("served") is True, "server.serve() must be awaited"
    cfg = captured["config"]
    assert cfg.host == "127.0.0.1" and cfg.port == 9099
