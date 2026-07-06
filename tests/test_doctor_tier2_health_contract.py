"""Regression: tier-2 (_probe_tier2) must accept the shared in-process
embedder's JSON /health contract, not only the legacy Rust plaintext "OK".

Background: the system migrated from the legacy Rust m3-embed-server (which
replies `OK` to GET /health) to the shared in-process embedder
(embed_server_inproc.py), which replies JSON
`{"status": "ok", "model": ..., "dim": ...}`. _probe_tier2 originally required
`body == "OK"` and stamped the healthy shared server as `unhealthy-200`,
turning the whole cascade `broken` even though the server embeds fine. These
tests pin the two accepted contracts + the genuinely-unhealthy case so the
stale-contract bug cannot silently return.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bin"))

from memory import doctor as _doctor  # noqa: E402


class _FakeResp:
    def __init__(self, status: int, body: str) -> None:
        self.status = status
        self._body = body.encode()

    def read(self) -> bytes:
        return self._body


class _FakeConn:
    """Stands in for http.client.HTTPConnection: returns a scripted response
    per requested path. /metrics defaults to 404 (the shared server has none)."""

    def __init__(self, responses: dict[str, _FakeResp]) -> None:
        self._responses = responses
        self._path: str | None = None

    def request(self, method: str, path: str) -> None:
        self._path = path

    def getresponse(self) -> _FakeResp:
        return self._responses.get(self._path, _FakeResp(404, '{"detail":"Not Found"}'))

    def close(self) -> None:
        pass


def _patch_conn(monkeypatch, responses: dict[str, _FakeResp]) -> None:
    monkeypatch.setattr(
        _doctor.http.client,
        "HTTPConnection",
        lambda host, port, timeout=None: _FakeConn(responses),
    )


def test_tier2_accepts_shared_json_health(monkeypatch):
    """Shared in-process embedder: JSON body with status 'ok' -> online.
    This is the exact response that used to be misclassified unhealthy-200."""
    _patch_conn(monkeypatch, {
        "/health": _FakeResp(
            200, '{"status":"ok","model":"bge-m3-GGUF-Q4_K_M.gguf","dim":1024}'
        ),
        # shared server has no /metrics — 404 is expected, must not fault
    })
    res = _doctor._probe_tier2()
    assert res["status"] == "online", res
    # model is surfaced from the /health JSON body (no /metrics on shared server)
    assert res["model"] == "bge-m3-GGUF-Q4_K_M.gguf", res


def test_tier2_accepts_shared_json_loading(monkeypatch):
    """status 'loading' (model still warming) is also healthy, matching
    shared_embedder_probe.py's contract."""
    _patch_conn(monkeypatch, {
        "/health": _FakeResp(200, '{"status":"loading"}'),
    })
    res = _doctor._probe_tier2()
    assert res["status"] == "online", res


def test_tier2_accepts_legacy_plaintext_ok(monkeypatch):
    """Backward compat: the legacy Rust m3-embed-server replies plaintext
    'OK' and still exposes /metrics — must remain online with model set."""
    _patch_conn(monkeypatch, {
        "/health": _FakeResp(200, "OK"),
        "/metrics": _FakeResp(200, '{"model":"legacy-rust-bge"}'),
    })
    res = _doctor._probe_tier2()
    assert res["status"] == "online", res
    assert res["model"] == "legacy-rust-bge", res


def test_tier2_rejects_non_healthy_json(monkeypatch):
    """A 200 whose JSON says the server is NOT ready must still be
    unhealthy — the fix widened the contract, it must not blanket-accept 200."""
    _patch_conn(monkeypatch, {
        "/health": _FakeResp(200, '{"status":"error"}'),
    })
    res = _doctor._probe_tier2()
    assert res["status"] == "unhealthy-200", res


def test_tier2_rejects_unparseable_200_body(monkeypatch):
    """A 200 with a non-JSON, non-'OK' body is unhealthy, not online."""
    _patch_conn(monkeypatch, {
        "/health": _FakeResp(200, "<html>gateway</html>"),
    })
    res = _doctor._probe_tier2()
    assert res["status"] == "unhealthy-200", res
