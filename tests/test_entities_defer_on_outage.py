"""Entity extraction must not charge rows a retry attempt while the LLM is down.

Measured 2026-10-03: with LM Studio up but no model loaded, every eligible row
failed, was recorded 'failed', and after MAX_ENTITY_ATTEMPTS was excluded from
extraction for good — 127 rows lost to "No models loaded". A server that cannot
answer is a SYSTEM state: the row is deferred, not failed.

Hermetic: the extractor, eligibility query, usability check and store are faked;
the queue writes are observed through a recording mc._db().
"""
from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

import llm_failover  # noqa: E402
import m3_entities  # noqa: E402

ROWS = [("m1", "first row long enough"), ("m2", "second row long enough"),
        ("m3", "third row long enough")]


class _RecordingConn:
    def __init__(self, sink):
        self.sink = sink

    def execute(self, sql, params=()):
        self.sink.append((" ".join(sql.split()), params))
        return self

    def executemany(self, sql, seq):
        for p in seq:
            self.execute(sql, p)
        return self

    def commit(self):
        pass


@pytest.fixture
def harness(monkeypatch):
    # _run_db writes these into os.environ; pre-set them so monkeypatch restores.
    monkeypatch.setenv("M3_DATABASE", "unused.db")
    monkeypatch.setenv("M3_ENABLE_ENTITY_GRAPH", "1")
    monkeypatch.setattr(llm_failover, "_OUTAGE_STATE", {})
    writes: list = []

    @contextmanager
    def fake_db(*a, **k):
        yield _RecordingConn(writes)

    monkeypatch.setattr(m3_entities.mc, "_db", fake_db)
    monkeypatch.setattr(m3_entities, "_ensure_extraction_status_column", lambda qc: None)
    monkeypatch.setattr(m3_entities, "_query_eligible_rows", lambda *a, **k: list(ROWS))

    calls = []

    def build(profile, token, vt, vp, client):
        async def extractor(body):
            calls.append(body)
            raise RuntimeError('http 400: {"error": {"message": "No models loaded."}}')
        return extractor

    monkeypatch.setattr(m3_entities, "_build_extractor", build)

    async def no_sleep(_s):
        return None

    monkeypatch.setattr(m3_entities.asyncio, "sleep", no_sleep)
    prof = SimpleNamespace(url="http://127.0.0.1:1234/v1/chat/completions",
                           timeout_s=5.0, input_max_chars=100)
    return SimpleNamespace(writes=writes, calls=calls, prof=prof)


def _run(h, counters):
    asyncio.run(m3_entities._run_db(
        Path("unused.db"), h.prof, "tok", frozenset(), frozenset(), ("note",),
        None, 1, None, True, counters))


def _failed_writes(writes):
    return [w for w in writes if "entity_extraction_queue" in w[0] and "'failed'" in w[0]]


def test_outage_defers_every_row_without_charging_an_attempt(harness, monkeypatch):
    async def down(client, token, *, fresh=False):
        raise llm_failover.LLMUnavailable("observed: no generative model listed")

    monkeypatch.setattr(m3_entities, "require_usable_endpoint", down, raising=False)
    counters = defaultdict(int)
    _run(harness, counters)
    assert counters["deferred"] == len(ROWS), dict(counters)
    assert counters["failed"] == 0, dict(counters)
    assert _failed_writes(harness.writes) == [], "no row may be recorded 'failed' during an outage"
    assert len(harness.calls) == 1, "after the first failure shows an outage, stop calling the model"


def test_a_real_row_failure_on_a_live_server_is_still_recorded(harness, monkeypatch):
    """The other side: with the server able to answer, a failing row goes down
    the normal path (counted, queued 'failed'), so real bad rows still surface."""
    async def up(client, token, *, fresh=False):
        return "http://127.0.0.1:1234/v1"

    monkeypatch.setattr(m3_entities, "require_usable_endpoint", up, raising=False)
    counters = defaultdict(int)
    _run(harness, counters)
    assert counters["deferred"] == 0, dict(counters)
    assert counters["failed"] == len(ROWS), dict(counters)
    assert len(_failed_writes(harness.writes)) == len(ROWS)


def test_dry_run_banner_only_on_a_dry_run(capsys):
    plan = {"profile_name": "p", "model": "m", "url": "u", "vocab_yaml": "v",
            "entity_types": [], "predicates": [], "types": None,
            "skip_already_extracted": True, "dbs": {}}
    m3_entities._print_dry_run(plan, dry_run=False)
    real = capsys.readouterr().out
    assert "DRY RUN" not in real and "writes WILL happen" in real
    m3_entities._print_dry_run(plan)
    assert "DRY RUN -- no writes will happen" in capsys.readouterr().out
